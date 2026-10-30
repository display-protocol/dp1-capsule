#!/usr/bin/env python3
"""dp1c: build and verify DP-1 preservation capsules (*.dp1c).

Prototype. Fills the two lines DP-1 v1.1.0 leaves blank:
  §9  Capsule layout     -> deterministic tar+zstd, sha256 = capsule id
  /meta/engine/<layer>   -> a pinned, flag-complete engine description
  §5  repro.frameHash    -> computed by a real headless render, offline

Commands
  build   WORKDIR --entry index.html --out X.dp1c [--title ..] [--provenance p.json] [--seed 0x..]
          packs assets, pins the engine, renders once with no network,
          writes repro.frameHash into playlist.json, embeds the reference render.
  verify  X.dp1c            extracts, re-renders offline, compares to the embedded frame hash.
  info    X.dp1c            prints playlist, engine pin, and reference render.
  render  WORKDIR --entry .. --png out.png     render a directory offline (debug).

Only stdlib + zstandard + Pillow + a chromium binary. No Feral File service anywhere.
"""
import argparse, hashlib, http.server, io, json, os, platform, shutil, socket, socketserver
import shlex, subprocess, sys, tarfile, tempfile, threading, time, uuid
from datetime import datetime, timezone

import zstandard
from PIL import Image

DP_VERSION = "1.1.0"
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")

# ---------- engine layer ----------------------------------------------------

DETERMINISTIC_FLAGS = [
    "--headless=new",
    "--disable-gpu",                 # software raster (SwiftShader): same pixels on any GPU
    "--hide-scrollbars",
    "--font-render-hinting=none",
    "--disable-lcd-text",
    "--run-all-compositor-stages-before-draw",
    "--deterministic-mode",
    "--enable-begin-frame-control",
    "--disable-extensions",
    "--disable-background-networking",
    "--no-first-run",
    "--no-default-browser-check",
    "--no-sandbox",
]

def chromium_version():
    out = subprocess.run([CHROMIUM, "--version"], capture_output=True, text=True).stdout.strip()
    # "Chromium 155.0.8059.84 Arch Linux" -> "155.0.8059.84"
    for tok in out.split():
        if tok[0].isdigit() and tok.count(".") == 3:
            return tok
    return out

def engine_pin(viewport, budget_ms):
    return {
        "engine": "chromium",
        "version": chromium_version(),
        "platform": f"{platform.system().lower()}-{platform.machine()}",
        "renderer": "software",
        "flags": DETERMINISTIC_FLAGS,
        "viewport": {"width": viewport[0], "height": viewport[1], "deviceScaleFactor": 1},
        "virtualTimeBudgetMs": budget_ms,
        "network": "none",
        "imageRef": None,   # the honest gap: a content-addressed runtime image goes here
    }

# ---------- engine image (pinned rootfs) -----------------------------------

ENGINE_CACHE = os.path.expanduser("~/.cache/dp1c/engines")

def engine_open(path_or_sha):
    """Return (rootfs_dir, engine_manifest). Accepts a rootfs dir, an engine-<sha>.tar.zst, or a bare sha in the cache."""
    if os.path.isdir(path_or_sha):
        root = path_or_sha
        man = json.load(open(os.path.join(root, "opt", "dp1c", "engine.json"))) if os.path.exists(os.path.join(root, "opt", "dp1c", "engine.json")) else None
        return root, man
    if os.path.isfile(path_or_sha):
        sha = sha256_file(path_or_sha)
        root = os.path.join(ENGINE_CACHE, sha, "rootfs")
        if not os.path.isdir(root):
            os.makedirs(root, exist_ok=True)
            subprocess.run(f"zstd -d -q -c {shlex.quote(path_or_sha)} | tar -x -C {shlex.quote(root)}", shell=True, check=True)
        side = os.path.splitext(os.path.splitext(path_or_sha)[0])[0] + ".json"
        man = json.load(open(side)) if os.path.exists(side) else None
        if man: json.dump(man, open(os.path.join(ENGINE_CACHE, sha, "engine.json"), "w"), indent=2)
        return root, man
    root = os.path.join(ENGINE_CACHE, path_or_sha, "rootfs")
    if os.path.isdir(root):
        mp = os.path.join(ENGINE_CACHE, path_or_sha, "engine.json")
        return root, (json.load(open(mp)) if os.path.exists(mp) else None)
    sys.exit(f"engine {path_or_sha} not found (not a dir, not a tar.zst, not in {ENGINE_CACHE})")

def engine_version(root):
    out = subprocess.run(["bwrap", "--ro-bind", root, "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
                          "--unshare-all", "--setenv", "HOME", "/tmp", "/usr/bin/chromium", "--version"],
                         capture_output=True, text=True).stdout.strip()
    for tok in out.split():
        if tok[0].isdigit() and tok.count(".") == 3: return tok
    return out

def render_in_engine(root, assets_dir, entry, engine, out_png):
    """Render inside the pinned rootfs. Network namespace is unshared at the kernel level (bwrap --unshare-net);
    loopback is brought up inside so the work is served over http from within the namespace."""
    vp = engine["viewport"]
    flags = " ".join(shlex.quote(f) for f in engine["flags"])
    inner = (f"ip link set lo up 2>/dev/null || true; "
             f"python -m http.server --bind 127.0.0.1 8731 -d /work >/dev/null 2>&1 & "
             f"for i in $(seq 50); do python -c 'import socket;socket.create_connection((\"127.0.0.1\",8731),0.1)' 2>/dev/null && break; sleep 0.1; done; "
             f"exec /usr/bin/chromium {flags} --window-size={vp['width']},{vp['height']} "
             f"--force-device-scale-factor={vp['deviceScaleFactor']} --virtual-time-budget={engine['virtualTimeBudgetMs']} "
             f"--user-data-dir=/tmp/profile --screenshot=/out/shot.png http://127.0.0.1:8731/{entry}")
    outdir = os.path.dirname(os.path.abspath(out_png)) or "."
    cmd = ["bwrap", "--ro-bind", root, "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
           "--ro-bind", os.path.abspath(assets_dir), "/work", "--bind", outdir, "/out",
           "--unshare-all", "--cap-add", "CAP_NET_ADMIN", "--die-with-parent", "--setenv", "HOME", "/tmp",
           "--setenv", "LANG", "C.UTF-8", "/bin/bash", "-c", inner]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    shot = os.path.join(outdir, "shot.png")
    if not os.path.exists(shot):
        sys.exit(f"engine render failed:\n{r.stderr[-3000:]}")
    if os.path.abspath(shot) != os.path.abspath(out_png): shutil.move(shot, out_png)
    return open(out_png, "rb").read()

# ---------- offline render (host chromium, fallback) ------------------------

class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass

def _serve(root):
    handler = lambda *a, **k: _Quiet(*a, directory=root, **k)
    srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
    srv.allow_reuse_address = True
    t = threading.Thread(target=srv.serve_forever, daemon=True); t.start()
    return srv, srv.server_address[1]

def render(assets_dir, entry, engine, out_png):
    """Render entry from assets_dir with every non-loopback host unresolvable. Returns png bytes."""
    srv, port = _serve(assets_dir)
    try:
        with tempfile.TemporaryDirectory() as prof:
            vp = engine["viewport"]
            cmd = [CHROMIUM, *engine["flags"],
                   f"--window-size={vp['width']},{vp['height']}",
                   f"--force-device-scale-factor={vp['deviceScaleFactor']}",
                   f"--virtual-time-budget={engine['virtualTimeBudgetMs']}",
                   '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
                   f"--user-data-dir={prof}",
                   f"--screenshot={out_png}",
                   f"http://127.0.0.1:{port}/{entry}"]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if not os.path.exists(out_png):
                sys.exit(f"render failed:\n{r.stderr[-2000:]}")
    finally:
        srv.shutdown(); srv.server_close()
    return open(out_png, "rb").read()

def dhash(png_bytes, size=8):
    im = Image.open(io.BytesIO(png_bytes)).convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(im.get_flattened_data()) if hasattr(im,'get_flattened_data') else list(im.getdata()); bits = []
    for row in range(size):
        for col in range(size):
            bits.append(px[row * (size + 1) + col] > px[row * (size + 1) + col + 1])
    return "0x" + int("".join("1" if b else "0" for b in bits), 2).to_bytes(size * size // 8, "big").hex()

def frame_hash(png_bytes):
    # hash the decoded pixels, not the PNG container (encoder metadata is not the artwork)
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    return {"sha256": hashlib.sha256(im.tobytes()).hexdigest(), "phash": dhash(png_bytes)}

# ---------- deterministic archive ------------------------------------------

def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""): h.update(chunk)
    return h.hexdigest()

def pack(stage_dir, out_path):
    """tar (sorted, mtime 0, uid/gid 0) + zstd(level 19, no checksum): same input -> same bytes."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for root, dirs, files in os.walk(stage_dir):
            dirs.sort()
            for name in sorted(files):
                full = os.path.join(root, name); arc = os.path.relpath(full, stage_dir)
                ti = tar.gettarinfo(full, arcname=arc)
                ti.mtime = 0; ti.uid = ti.gid = 0; ti.uname = ti.gname = ""
                ti.mode = 0o644
                with open(full, "rb") as f: tar.addfile(ti, f)
    cctx = zstandard.ZstdCompressor(level=19, write_checksum=False, write_content_size=True)
    data = cctx.compress(buf.getvalue())
    with open(out_path, "wb") as f: f.write(data)
    return hashlib.sha256(data).hexdigest()

def unpack(capsule_path, dest):
    with open(capsule_path, "rb") as f:
        raw = zstandard.ZstdDecompressor().stream_reader(f).read()
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        for m in tar.getmembers():
            if m.name.startswith("/") or ".." in m.name.split("/"):
                sys.exit(f"unsafe entry {m.name}")
        tar.extractall(dest, filter="data")

# ---------- commands --------------------------------------------------------

def cmd_build(a):
    work = os.path.abspath(a.workdir)
    if not os.path.exists(os.path.join(work, a.entry)): sys.exit(f"entry {a.entry} not in {work}")
    viewport = tuple(int(x) for x in a.viewport.split("x"))
    engine = engine_pin(viewport, a.budget)
    root = None
    if a.engine:
        root, man = engine_open(a.engine)
        engine["version"] = engine_version(root)
        engine["imageRef"] = (man or {}).get("imageRef") or {"sha256": None, "note": "engine dir given without manifest"}
        engine["flags"] = [f for f in engine["flags"]]  # same flag set inside the engine

    with tempfile.TemporaryDirectory() as stage:
        assets = os.path.join(stage, "assets"); shutil.copytree(work, assets)
        asset_hashes = {}
        for root, _, files in os.walk(assets):
            for n in files:
                p = os.path.join(root, n); asset_hashes[os.path.relpath(p, assets)] = sha256_file(p)

        os.makedirs(os.path.join(stage, "meta", "engine")); os.makedirs(os.path.join(stage, "meta", "render"))
        png = os.path.join(stage, "meta", "render", "reference.png")
        t0 = time.time()
        png_bytes = render_in_engine(root, assets, a.entry, engine, png) if root else render(assets, a.entry, engine, png)
        dt = time.time() - t0
        fh = frame_hash(png_bytes)

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        # ids derive from content so the same work packs to the same bytes every time
        fingerprint = (a.source or a.entry) + "|" + "|".join(f"{k}={asset_hashes[k]}" for k in sorted(asset_hashes))
        item_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "dp1c:item:" + fingerprint))
        item = {
            "id": item_id,
            "title": a.title,
            "source": a.source or f"dp1c:assets/{a.entry}",
            "duration": a.duration,
            "license": a.license,
            "repro": {
                "engineVersion": {"chromium": engine["version"]},
                **({"seed": a.seed} if a.seed else {}),
                "assetsSHA256": [asset_hashes[k] for k in sorted(asset_hashes)],
                "frameHash": fh,
            },
        }
        if a.provenance:
            item["provenance"] = json.load(open(a.provenance))
        playlist = {"dpVersion": DP_VERSION, "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "dp1c:playlist:" + fingerprint)), "title": a.title,
                    "created": a.created or now, "defaults": {"display": {"scaling": "fit", "background": "#000000"}},
                    "items": [item]}
        json.dump(playlist, open(os.path.join(stage, "playlist.json"), "w"), indent=2, sort_keys=True)
        json.dump(engine, open(os.path.join(stage, "meta", "engine", "chromium.json"), "w"), indent=2)
        json.dump({"entry": a.entry, "assets": asset_hashes, "renderedAt": a.created or now,
                   "frameHash": fh, "png": "meta/render/reference.png"},
                  open(os.path.join(stage, "meta", "render", "reference.json"), "w"), indent=2)
        cid = pack(stage, a.out)
    print(json.dumps({"capsule": a.out, "capsuleId": cid, "frameHash": fh, "engine": engine["version"],
                      "engineImage": (engine.get("imageRef") or {}).get("sha256"),
                      "renderSeconds": round(dt, 1), "assets": len(asset_hashes)}, indent=2))

def cmd_verify(a):
    with tempfile.TemporaryDirectory() as d:
        unpack(a.capsule, d)
        ref = json.load(open(os.path.join(d, "meta", "render", "reference.json")))
        engine = json.load(open(os.path.join(d, "meta", "engine", "chromium.json")))
        pl = json.load(open(os.path.join(d, "playlist.json")))
        assets = os.path.join(d, "assets")
        bad = [k for k, h in ref["assets"].items() if sha256_file(os.path.join(assets, k)) != h]
        want_img = (engine.get("imageRef") or {}).get("sha256")
        root = None
        if a.engine:
            root, _ = engine_open(a.engine)
        elif want_img and os.path.isdir(os.path.join(ENGINE_CACHE, want_img, "rootfs")):
            root = os.path.join(ENGINE_CACHE, want_img, "rootfs")
        elif want_img and not a.host:
            sys.exit(f"capsule is pinned to engine image {want_img}; it is not in {ENGINE_CACHE} and no --engine was given. "
                     f"Pass --engine <engine-{want_img}.tar.zst>, or --host to verify with this machine's chromium (weaker claim).")
        if root and want_img and a.engine and os.path.isfile(a.engine) and sha256_file(a.engine) != want_img:
            sys.exit(f"engine image mismatch: capsule wants {want_img}, given {sha256_file(a.engine)}")
        here = engine_version(root) if root else chromium_version()
        png = render_in_engine(root, assets, ref["entry"], engine, os.path.join(d, "verify.png")) if root \
              else render(assets, ref["entry"], engine, os.path.join(d, "verify.png"))
        fh = frame_hash(png)
        if a.png: shutil.copy(os.path.join(d, "verify.png"), a.png)
        want = pl["items"][0]["repro"]["frameHash"]
        match = fh["sha256"] == want["sha256"]
        ham = bin(int(fh["phash"], 16) ^ int(want["phash"], 16)).count("1")
        print(json.dumps({"capsuleId": sha256_file(a.capsule), "assetsIntact": not bad, "corruptAssets": bad,
                          "enginePinned": engine["version"], "engineHere": here,
                          "engineImagePinned": want_img, "ranInsideEngine": bool(root),
                          "frameHashExpected": want["sha256"], "frameHashHere": fh["sha256"],
                          "exact": match, "phashDistance": ham,
                          "verdict": "reproduced" if match else ("near" if ham <= 4 else "reproMismatch")}, indent=2))
        sys.exit(0 if match else 2)

def cmd_info(a):
    with tempfile.TemporaryDirectory() as d:
        unpack(a.capsule, d)
        for p in ("playlist.json", "meta/engine/chromium.json", "meta/render/reference.json"):
            print(f"--- {p}"); print(open(os.path.join(d, p)).read())

def cmd_render(a):
    engine = engine_pin(tuple(int(x) for x in a.viewport.split("x")), a.budget)
    b = render(os.path.abspath(a.workdir), a.entry, engine, os.path.abspath(a.png))
    print(json.dumps(frame_hash(b)))

def main():
    pass
    p = argparse.ArgumentParser(prog="dp1c"); sp = p.add_subparsers(dest="cmd", required=True)
    b = sp.add_parser("build"); b.add_argument("workdir"); b.add_argument("--entry", default="index.html")
    b.add_argument("--out", required=True); b.add_argument("--title", default="Untitled")
    b.add_argument("--source"); b.add_argument("--provenance"); b.add_argument("--seed"); b.add_argument("--created", help="fixed timestamp for reproducible packing")
    b.add_argument("--license", default="token"); b.add_argument("--duration", type=int, default=300)
    b.add_argument("--viewport", default="1920x1080"); b.add_argument("--budget", type=int, default=10000)
    b.add_argument("--engine", help="pinned engine: rootfs dir, engine-<sha>.tar.zst, or cached sha")
    b.set_defaults(f=cmd_build)
    v = sp.add_parser("verify"); v.add_argument("capsule"); v.add_argument("--png")
    v.add_argument("--engine", help="engine image to run inside (checked against the capsule's imageRef)")
    v.add_argument("--host", action="store_true", help="allow this machine's chromium when the pinned engine is absent")
    v.set_defaults(f=cmd_verify)
    i = sp.add_parser("info"); i.add_argument("capsule"); i.set_defaults(f=cmd_info)
    r = sp.add_parser("render"); r.add_argument("workdir"); r.add_argument("--entry", default="index.html")
    r.add_argument("--png", required=True); r.add_argument("--viewport", default="1920x1080")
    r.add_argument("--budget", type=int, default=10000); r.set_defaults(f=cmd_render)
    a = p.parse_args(); a.f(a)

if __name__ == "__main__":
    main()
