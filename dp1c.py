#!/usr/bin/env python3
"""dp1c: build and verify DP-1 preservation capsules (*.dp1c).

Prototype. Fills the lines DP-1 v1.1.0 leaves blank:
  §9  capsule layout      -> deterministic tar+zstd, sha256 = capsule id
  /meta/engine/<layer>    -> a pinned engine description with a content-addressed imageRef
  §5  repro.frameHash     -> computed by harness.py over the DevTools protocol, inside the pinned engine,
                             with the network unshared, the clock pinned, the viewport set before navigation

Commands
  build   WORKDIR --engine ENGINE --out X.dp1c [--entry index.html] [--title ..] [--source URL]
          [--provenance p.json] [--seed 0x..] [--created ISO] [--host]
  verify  X.dp1c [--engine ENGINE] [--host] [--png out.png]
  info    X.dp1c

ENGINE is an engine-<sha>.tar.zst from engine/build-engine.sh, a bare sha already in the cache, or a rootfs
directory (unpinned: reported as a different engine). --host runs this machine's chromium outside the engine
sandbox; it is a weaker claim and is always reported as such.
Only stdlib + zstandard + Pillow + bubblewrap. No Feral File service anywhere.
"""
import argparse, base64, hashlib, io, json, os, platform, shlex, shutil, subprocess, sys, tarfile, tempfile, uuid
from datetime import datetime, timezone

import zstandard
from PIL import Image

DP_VERSION = "1.1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "harness.py")
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
ENGINE_CACHE = os.path.expanduser("~/.cache/dp1c/engines")
MAX_CAPSULE_BYTES = 2 * 1024 ** 3
MAX_MEMBERS = 50000
VIRTUAL_EPOCH = 1_600_000_000  # 2020-09-13T12:26:40Z: the clock every render starts from

# ---------- the engine pin ---------------------------------------------------

DETERMINISTIC_FLAGS = [
    "--headless=new",
    "--disable-gpu",                 # software raster: pixels do not depend on a GPU or driver
    "--use-angle=swiftshader",       # and name the software backend instead of relying on the fallback
    "--use-gl=angle",
    "--force-color-profile=srgb",
    "--disable-skia-runtime-opts",
    "--hide-scrollbars",
    "--font-render-hinting=none",
    "--disable-lcd-text",
    "--run-all-compositor-stages-before-draw",
    "--deterministic-mode",
    "--enable-begin-frame-control",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-crashpad",
    "--disable-crash-reporter",
    "--disable-dev-shm-usage",
    "--no-first-run",
    "--no-default-browser-check",
    "--no-sandbox",                  # the sandbox is bubblewrap's, outside the browser
    "--user-data-dir=/tmp/profile",
]
SANDBOX_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "TMPDIR": "/tmp", "XDG_CONFIG_HOME": "/tmp/xdg",
               "XDG_CACHE_HOME": "/tmp/xdgc", "XDG_RUNTIME_DIR": "/tmp/run", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC"}

def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""): h.update(chunk)
    return h.hexdigest()

def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

def engine_pin(version, viewport, budget_ms, image_ref):
    return {
        "engine": "chromium", "version": version, "platform": "linux-x86_64", "renderer": "software",
        "flags": list(DETERMINISTIC_FLAGS), "env": dict(SANDBOX_ENV),
        "viewport": {"width": viewport[0], "height": viewport[1], "deviceScaleFactor": 1},
        "virtualEpochSeconds": VIRTUAL_EPOCH, "virtualTimeBudgetMs": budget_ms, "timezone": "UTC", "locale": "en-US",
        "network": "none",
        "harnessSha256": sha256_file(HARNESS),
        "imageRef": image_ref,
    }

# ---------- engine image ----------------------------------------------------

def engine_open(spec):
    """Return (rootfs, image_sha_or_None, manifest_or_None). Only a tar.zst or a completed cache entry has a sha."""
    if os.path.isdir(spec):
        mp = os.path.join(spec, "opt", "dp1c", "engine.json")
        return _mountpoints(spec), None, (json.load(open(mp)) if os.path.exists(mp) else None)
    if os.path.isfile(spec):
        sha = sha256_file(spec)
        side = spec[:-len(".tar.zst")] + ".json" if spec.endswith(".tar.zst") else None
        man = json.load(open(side)) if side and os.path.exists(side) else None
        root = _cache_root(sha)
        if root is None:
            final = os.path.join(ENGINE_CACHE, sha); tmp = final + ".partial"
            _rmtree(tmp); _rmtree(final)   # an incomplete entry is garbage
            os.makedirs(os.path.join(tmp, "rootfs"))
            subprocess.run(f"zstd -d -q -c {shlex.quote(spec)} | tar -x -C {shlex.quote(os.path.join(tmp, 'rootfs'))}",
                           shell=True, check=True)
            if man: json.dump(man, open(os.path.join(tmp, "engine.json"), "w"), indent=2, sort_keys=True)
            open(os.path.join(tmp, "COMPLETE"), "w").write(sha + "\n")
            os.replace(tmp, final); root = os.path.join(final, "rootfs")
        return _mountpoints(root), sha, man or _cache_manifest(sha)
    root = _cache_root(spec)
    if root: return _mountpoints(root), spec, _cache_manifest(spec)
    sys.exit(f"engine {spec} not found (not a dir, not a tar.zst, not a completed entry in {ENGINE_CACHE})")

def _rmtree(path):
    """rmtree that first makes every directory writable (a rootfs has 0555 directories)."""
    if not os.path.lexists(path): return
    for dirpath, dirs, _ in os.walk(path):
        for d in dirs:
            p = os.path.join(dirpath, d)
            if not os.path.islink(p):
                try: os.chmod(p, 0o700)
                except OSError: pass
    try: os.chmod(path, 0o700)
    except OSError: pass
    shutil.rmtree(path)

def _cache_root(sha):
    d = os.path.join(ENGINE_CACHE, sha)
    if os.path.isfile(os.path.join(d, "COMPLETE")) and open(os.path.join(d, "COMPLETE")).read().strip() == sha:
        return os.path.join(d, "rootfs")
    return None

def _cache_manifest(sha):
    mp = os.path.join(ENGINE_CACHE, sha, "engine.json")
    return json.load(open(mp)) if os.path.exists(mp) else None

def _mountpoints(root):
    for m in ("dev", "proc", "tmp", "work", "out", "harness"): os.makedirs(os.path.join(root, m), exist_ok=True)
    return root

def _env_args():
    out = ["--clearenv"]
    for k, v in SANDBOX_ENV.items(): out += ["--setenv", k, v]
    return out

def engine_version(root):
    out = subprocess.run(["bwrap", "--ro-bind", root, "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
                          "--unshare-all", *_env_args(), "/usr/bin/chromium", "--version"],
                         capture_output=True, text=True).stdout.strip()
    return _ver(out)

def host_version():
    if not CHROMIUM: sys.exit("--host asked for but no chromium binary on PATH")
    return _ver(subprocess.run([CHROMIUM, "--version"], capture_output=True, text=True).stdout)

def _ver(s):
    for tok in s.split():
        if tok[0].isdigit() and tok.count(".") == 3: return tok
    return s.strip()

# ---------- render ----------------------------------------------------------

def render(root, assets_dir, entry, engine, outdir):
    """Run harness.py under bubblewrap with the network namespace unshared. root is the pinned engine rootfs, or None
    for the host's own filesystem and chromium (--host: a different-engine comparison, never 'reproduced').
    Writes shot.png + render.json into outdir, a fresh directory that is not inside the capsule."""
    for f in ("shot.png", "render.json"):
        if os.path.exists(os.path.join(outdir, f)): os.remove(os.path.join(outdir, f))
    hdir = os.path.join(outdir, "_harness"); os.makedirs(hdir, exist_ok=True); shutil.copy(HARNESS, os.path.join(hdir, "harness.py"))
    if root == "CONTAINER":
        # we are already inside the engine image (verify --container); the container runtime isolates the network
        cfg = {"out": outdir, "work": os.path.abspath(assets_dir), "entry": entry, "chromium": "/usr/bin/chromium",
               "flags": engine["flags"], "viewport": engine["viewport"], "virtualEpochSeconds": engine["virtualEpochSeconds"],
               "virtualTimeBudgetMs": engine["virtualTimeBudgetMs"], "timezone": engine["timezone"], "locale": engine["locale"],
               "cwd": "/tmp", "chromiumPrefix": []}
        env = dict(SANDBOX_ENV); os.makedirs("/tmp/xdg", exist_ok=True); os.makedirs("/tmp/xdgc", exist_ok=True); os.makedirs("/tmp/run", exist_ok=True); os.makedirs("/tmp/profile", exist_ok=True)
        r = subprocess.run([sys.executable, HARNESS], input=canon(cfg), capture_output=True, text=True, timeout=1800, env=env)
        shot = os.path.join(outdir, "shot.png"); rj = os.path.join(outdir, "render.json")
        if r.returncode != 0 or not os.path.isfile(shot) or not os.path.isfile(rj):
            sys.exit(f"render failed (exit {r.returncode}):\n{r.stderr[-3000:]}")
        return open(shot, "rb").read(), json.load(open(rj))
    if root:
        fs = ["--ro-bind", root, "/"]; W, O, H = "/work", "/out", "/harness"; chromium, python = "/usr/bin/chromium", "/usr/bin/python"
    else:
        if not CHROMIUM: sys.exit("--host asked for but no chromium binary on PATH")
        fs = ["--ro-bind", "/", "/", "--tmpfs", "/mnt", "--dir", "/mnt/work", "--dir", "/mnt/out", "--dir", "/mnt/harness"]
        W, O, H = "/mnt/work", "/mnt/out", "/mnt/harness"; chromium, python = CHROMIUM, sys.executable
    setpriv = os.path.join(root or "", "usr/bin/setpriv") if root else shutil.which("setpriv")
    cfg = {"out": O, "work": W, "entry": entry, "chromium": chromium, "flags": engine["flags"],
           "viewport": engine["viewport"], "virtualEpochSeconds": engine["virtualEpochSeconds"],
           "virtualTimeBudgetMs": engine["virtualTimeBudgetMs"], "timezone": engine["timezone"], "locale": engine["locale"],
           "cwd": "/tmp",
           # after loopback is up the harness launches chromium with every capability dropped
           "chromiumPrefix": ["/usr/bin/setpriv", "--inh-caps=-all", "--bounding-set=-all", "--no-new-privs"] if (setpriv and os.path.exists(setpriv)) else []}
    cmd = ["bwrap", *fs, "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
           "--ro-bind", os.path.abspath(assets_dir), W, "--bind", os.path.abspath(outdir), O, "--ro-bind", hdir, H,
           "--unshare-all", "--uid", "0", "--gid", "0", "--cap-add", "CAP_NET_ADMIN", "--cap-add", "CAP_SETPCAP", "--chdir", "/", "--die-with-parent",
           *_env_args(), "/bin/bash", "-c", f"mkdir -p /tmp/xdg /tmp/xdgc /tmp/run /tmp/profile && exec {shlex.quote(python)} {H}/harness.py"]
    if os.environ.get("DP1C_DEBUG"): print("bwrap:", " ".join(shlex.quote(c) for c in cmd), file=sys.stderr)
    r = subprocess.run(cmd, input=canon(cfg), capture_output=True, text=True, timeout=1800)
    shot = os.path.join(outdir, "shot.png"); rj = os.path.join(outdir, "render.json")
    if r.returncode != 0 or not os.path.isfile(shot) or not os.path.isfile(rj):
        sys.exit(f"render failed (exit {r.returncode}):\n{r.stderr[-3000:]}")
    return open(shot, "rb").read(), json.load(open(rj))

def render_problems(rj):
    d = rj["diagnostics"]
    http = [e for e in d["httpErrors"] if not e["url"].endswith("/favicon.ico")]
    return {"loadFailures": d["loadFailures"], "httpErrors": http, "exceptions": d["exceptions"]}

def dhash(im, size=8):
    g = im.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(g.get_flattened_data()) if hasattr(g, "get_flattened_data") else list(g.getdata())
    bits = "".join("1" if px[r * (size + 1) + c] > px[r * (size + 1) + c + 1] else "0" for r in range(size) for c in range(size))
    return "0x" + int(bits, 2).to_bytes(size * size // 8, "big").hex()

def frame_hash(png_bytes, viewport):
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    if im.size != (viewport["width"], viewport["height"]):
        sys.exit(f"captured frame is {im.size}, pin says {viewport['width']}x{viewport['height']}")
    h = hashlib.sha256(f"{im.width}x{im.height}:RGB8:".encode()); h.update(im.tobytes())
    return {"sha256": h.hexdigest(), "phash": dhash(im), "width": im.width, "height": im.height, "format": "RGB8"}

# ---------- archive ---------------------------------------------------------

def walk_files(root):
    out = []
    for dirpath, _, files in os.walk(root):
        for n in files: out.append(os.path.relpath(os.path.join(dirpath, n), root))
    return sorted(out)

def pack(stage_dir, out_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for arc in walk_files(stage_dir):
            full = os.path.join(stage_dir, arc)
            ti = tar.gettarinfo(full, arcname=arc)
            ti.mtime = 0; ti.uid = ti.gid = 0; ti.uname = ti.gname = ""; ti.mode = 0o644
            with open(full, "rb") as f: tar.addfile(ti, f)
    data = zstandard.ZstdCompressor(level=19, write_checksum=False, write_content_size=True).compress(buf.getvalue())
    with open(out_path, "wb") as f: f.write(data)
    return hashlib.sha256(data).hexdigest()

def unpack(capsule_path, dest):
    if os.path.getsize(capsule_path) > MAX_CAPSULE_BYTES: sys.exit("capsule too large")
    with open(capsule_path, "rb") as f:
        raw = zstandard.ZstdDecompressor(max_window_size=2 ** 31).stream_reader(f).read(MAX_CAPSULE_BYTES + 1)
    if len(raw) > MAX_CAPSULE_BYTES: sys.exit("capsule decompresses beyond the limit")
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        members = tar.getmembers()
        if len(members) > MAX_MEMBERS: sys.exit("too many members")
        for m in members:
            if not (m.isreg() or m.isdir()): sys.exit(f"capsule may only contain files and directories: {m.name}")
            if m.name.startswith("/") or ".." in m.name.split("/"): sys.exit(f"unsafe entry {m.name}")
        tar.extractall(dest, filter="data")

def check_assets(assets_dir, manifest):
    """Every regular file under assets/ must be in the manifest with a matching hash, and vice versa."""
    present = walk_files(assets_dir)
    for k in manifest:
        if k.startswith("/") or ".." in k.split("/"): return ["unsafe manifest key " + k]
    bad = [k for k in sorted(set(present) | set(manifest))
           if k not in manifest or k not in present or sha256_file(os.path.join(assets_dir, k)) != manifest[k]]
    return bad

# ---------- whole-capsule signature (DP-1 §7 object shape, over the capsule id) ----------

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"

def b58encode(b):
    n = int.from_bytes(b, "big"); out = ""
    while n: n, r = divmod(n, 58); out = B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out

def b58decode(s):
    n = 0
    for ch in s: n = n * 58 + B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(s) - len(s.lstrip("1"))) + raw

def did_key_from_pub(pub_bytes):
    return "did:key:z" + b58encode(b"\xed\x01" + pub_bytes)       # multicodec ed25519-pub

def pub_from_did_key(did):
    if not did.startswith("did:key:z"): raise ValueError("only did:key with base58btc is supported")
    raw = b58decode(did[len("did:key:z"):])
    if raw[:2] != b"\xed\x01" or len(raw) != 34: raise ValueError("not an ed25519 did:key")
    return raw[2:]

def sig_sidecar(capsule_path): return capsule_path + ".sig.json"

def b64u(b): return base64.urlsafe_b64encode(b).rstrip(b"=").decode()
def b64u_dec(s): return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def cmd_keygen(a):
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization
    if os.path.exists(a.key): sys.exit(f"{a.key} exists; not overwriting a key")
    k = ed25519.Ed25519PrivateKey.generate()
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    fd = os.open(a.key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600); os.write(fd, pem); os.close(fd)
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    print(json.dumps({"key": a.key, "kid": did_key_from_pub(pub)}, indent=2))

def cmd_sign(a):
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization
    k = serialization.load_pem_private_key(open(a.key, "rb").read(), password=None)
    if not isinstance(k, ed25519.Ed25519PrivateKey): sys.exit("key is not ed25519")
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    cid = sha256_file(a.capsule); payload_hash = "sha256:" + cid
    entry = {"alg": "ed25519", "kid": did_key_from_pub(pub), "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
             "payload_hash": payload_hash, "role": a.role, "sig": b64u(k.sign(payload_hash.encode()))}
    side = sig_sidecar(a.capsule)
    doc = json.load(open(side)) if os.path.exists(side) else {"capsuleId": cid, "signatures": []}
    if doc.get("capsuleId") != cid: sys.exit(f"{side} belongs to a different capsule ({doc.get('capsuleId')})")
    doc["signatures"] = [x for x in doc["signatures"] if x["kid"] != entry["kid"]] + [entry]
    open(side, "w").write(canon(doc))
    print(json.dumps({"capsuleId": cid, "sidecar": side, "kid": entry["kid"], "role": a.role}, indent=2))

def check_signatures(capsule_path, sig_path=None):
    """Return (list of {kid, role, ts, valid, reason}) for the sidecar, or [] if there is none."""
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.exceptions import InvalidSignature
    side = sig_path or sig_sidecar(capsule_path)
    if not os.path.exists(side): return []
    cid = sha256_file(capsule_path); doc = json.load(open(side)); out = []
    for x in doc.get("signatures", []):
        r = {"kid": x.get("kid"), "role": x.get("role"), "ts": x.get("ts"), "valid": False, "reason": ""}
        try:
            if doc.get("capsuleId") != cid or x.get("payload_hash") != "sha256:" + cid: raise ValueError("payload_hash is not this capsule's id")
            if x.get("alg") != "ed25519": raise ValueError("unsupported alg")
            ed25519.Ed25519PublicKey.from_public_bytes(pub_from_did_key(x["kid"])).verify(b64u_dec(x["sig"]), x["payload_hash"].encode())
            r["valid"] = True
        except (InvalidSignature, ValueError, KeyError) as e:
            r["reason"] = str(e) or type(e).__name__
        out.append(r)
    return out

# ---------- commands --------------------------------------------------------

def cmd_build(a):
    work = os.path.abspath(a.workdir)
    if not os.path.isfile(os.path.join(work, a.entry)): sys.exit(f"entry {a.entry} not in {work}")
    if "/" in a.entry or a.entry.startswith("."): sys.exit("entry must be a plain file name at the asset root")
    viewport = tuple(int(x) for x in a.viewport.split("x"))
    if a.engine:
        root, sha, man = engine_open(a.engine)
        image_ref = (man or {}).get("imageRef") or {}
        image_ref = {**image_ref, "sha256": sha}        # the computed digest is authoritative; the sidecar only adds packages
        version = engine_version(root)
    elif a.host:
        root, version, image_ref = None, host_version(), None
    else:
        sys.exit("build needs --engine (pinned) or --host (unpinned, weaker claim)")
    engine = engine_pin(version, viewport, a.budget, image_ref)

    with tempfile.TemporaryDirectory() as stage, tempfile.TemporaryDirectory() as outdir:
        assets = os.path.join(stage, "assets"); shutil.copytree(work, assets)
        asset_hashes = {k: sha256_file(os.path.join(assets, k)) for k in walk_files(assets)}
        png, rj = render(root, assets, a.entry, engine, outdir)
        problems = render_problems(rj)
        if any(problems.values()) and not a.allow_render_errors:
            sys.exit("the work did not render cleanly; capsule not written:\n" + canon(problems) +
                     "\n(pass --allow-render-errors to record them in the capsule anyway)")
        fh = frame_hash(png, engine["viewport"])
        now = a.created or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        fingerprint = (a.source or a.entry) + "|" + "|".join(f"{k}={asset_hashes[k]}" for k in sorted(asset_hashes))
        item = {"id": str(uuid.uuid5(uuid.NAMESPACE_URL, "dp1c:item:" + fingerprint)), "title": a.title,
                "source": a.source or f"dp1c:assets/{a.entry}", "duration": a.duration, "license": a.license,
                "repro": {"engineVersion": {"chromium": version}, **({"seed": a.seed} if a.seed else {}),
                          "assetsSHA256": [asset_hashes[k] for k in sorted(asset_hashes)],
                          "frameHash": {"sha256": fh["sha256"], "phash": fh["phash"]}}}
        if a.provenance: item["provenance"] = json.load(open(a.provenance))
        playlist = {"dpVersion": DP_VERSION, "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "dp1c:playlist:" + fingerprint)),
                    "title": a.title, "created": now, "defaults": {"display": {"scaling": "fit", "background": "#000000"}},
                    "items": [item]}
        os.makedirs(os.path.join(stage, "meta", "engine")); os.makedirs(os.path.join(stage, "meta", "render"))
        open(os.path.join(stage, "playlist.json"), "w").write(canon(playlist))
        open(os.path.join(stage, "meta", "engine", "chromium.json"), "w").write(canon(engine))
        open(os.path.join(stage, "meta", "render", "reference.json"), "w").write(canon(
            {"entry": a.entry, "assets": asset_hashes, "renderedAt": now, "frame": fh, "page": rj["page"],
             "glRenderer": rj.get("glRenderer"), "problems": problems, "png": "meta/render/reference.png"}))
        shutil.copy(os.path.join(outdir, "shot.png"), os.path.join(stage, "meta", "render", "reference.png"))
        cid = pack(stage, a.out)
    print(json.dumps({"capsule": a.out, "capsuleId": cid, "frameHash": fh["sha256"], "phash": fh["phash"],
                      "engine": version, "engineImage": (image_ref or {}).get("sha256"),
                      "ranInsideEngine": root is not None, "problems": problems, "assets": len(asset_hashes)}, indent=2))

def cmd_verify(a):
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outdir:
        unpack(a.capsule, d)
        pl = json.load(open(os.path.join(d, "playlist.json")))
        engine = json.load(open(os.path.join(d, "meta", "engine", "chromium.json")))
        ref = json.load(open(os.path.join(d, "meta", "render", "reference.json")))
        if len(pl.get("items", [])) != 1: sys.exit("this verifier handles exactly one item per capsule")
        item = pl["items"][0]
        want = item["repro"]["frameHash"]
        # 1. assets: whole tree vs manifest vs playlist
        bad = check_assets(os.path.join(d, "assets"), ref["assets"])
        if sorted(ref["assets"].values()) != sorted(item["repro"]["assetsSHA256"]): bad.append("playlist repro.assetsSHA256 disagrees with reference.json")
        # the three copies of the reference frame must agree: playlist, reference.json, and the PNG itself
        ref_png = frame_hash(open(os.path.join(d, "meta", "render", "reference.png"), "rb").read(), engine["viewport"])
        if not (ref_png["sha256"] == ref["frame"]["sha256"] == want["sha256"] and ref_png["phash"] == ref["frame"]["phash"] == want["phash"]):
            bad.append("reference frame records disagree (playlist / reference.json / reference.png)")
        # 2. engine: computed digest is the only thing trusted
        pinned_img = (engine.get("imageRef") or {}).get("sha256")
        root, here_img = None, None
        if a.container:
            here_img = os.environ.get("DP1C_CONTAINER_ENGINE_SHA")
            if not here_img: sys.exit("--container needs DP1C_CONTAINER_ENGINE_SHA (the launcher's digest of the imported image)")
            root = "CONTAINER"
        elif a.engine:
            root, here_img, _ = engine_open(a.engine)
        elif pinned_img and _cache_root(pinned_img):
            root, here_img = _cache_root(pinned_img), pinned_img
        elif not a.host:
            sys.exit(f"capsule is pinned to engine image {pinned_img}; it is not in {ENGINE_CACHE} and no --engine was given. "
                     f"Pass --engine <engine-{pinned_img}.tar.zst>, or --host to compare against this machine's chromium (weaker claim).")
        engine_match = bool(root) and here_img is not None and here_img == pinned_img
        if root and not engine_match and not a.allow_engine_mismatch:
            sys.exit(f"engine mismatch: capsule pinned to {pinned_img}, given {here_img or 'an unpinned directory'} "
                     f"(pass --allow-engine-mismatch to run anyway; the result is then a different-engine comparison)")
        harness_match = engine.get("harnessSha256") == sha256_file(HARNESS)
        # 3. render with the pin's settings; flags come from this tool, never from the capsule
        run_engine = {**engine, "flags": list(DETERMINISTIC_FLAGS), "env": dict(SANDBOX_ENV)}
        here_ver = _ver(subprocess.run(["/usr/bin/chromium", "--version"], capture_output=True, text=True).stdout) if root == "CONTAINER" \
                   else (engine_version(root) if root else host_version())
        png, rj = render(root, os.path.join(d, "assets"), ref["entry"], run_engine, outdir)
        if a.png: shutil.copy(os.path.join(outdir, "shot.png"), a.png)
        fh = frame_hash(png, engine["viewport"])
        exact = fh["sha256"] == want["sha256"]
        ham = bin(int(fh["phash"], 16) ^ int(want["phash"], 16)).count("1")
        sigs = check_signatures(a.capsule, a.sig)
        signer_ok = True
        if a.require_signer:
            signer_ok = any(x["valid"] and x["kid"] == a.require_signer for x in sigs)
        if bad: verdict = "assetsCorrupt"
        elif not signer_ok: verdict = "unsigned" if not sigs else "signatureInvalid"
        elif exact: verdict = "reproduced" if (engine_match and harness_match) else "reproducedOnDifferentEngine"
        elif ham <= 4 and not engine_match: verdict = "nearOnDifferentEngine"
        else: verdict = "reproMismatch"
        print(json.dumps({"capsuleId": sha256_file(a.capsule), "assetsIntact": not bad, "assetProblems": bad,
                          "engineImagePinned": pinned_img, "engineImageHere": here_img, "engineMatch": engine_match,
                          "enginePinned": engine["version"], "engineHere": here_ver, "ranInsideEngine": root is not None,
                          "sandbox": "container" if root == "CONTAINER" else ("bwrap" if root else "bwrap-host"),
                          "harnessMatch": harness_match,
                          "frameHashExpected": want["sha256"], "frameHashHere": fh["sha256"], "exact": exact,
                          "phashDistance": ham, "renderProblems": render_problems(rj),
                          "signatures": sigs, "requiredSigner": a.require_signer, "verdict": verdict}, indent=2))
        sys.exit(0 if verdict == "reproduced" else 2)

def cmd_info(a):
    with tempfile.TemporaryDirectory() as d:
        unpack(a.capsule, d)
        for p in ("playlist.json", "meta/engine/chromium.json", "meta/render/reference.json"):
            print(f"--- {p}"); print(json.dumps(json.load(open(os.path.join(d, p))), indent=2, sort_keys=True))

def main():
    p = argparse.ArgumentParser(prog="dp1c"); sp = p.add_subparsers(dest="cmd", required=True)
    b = sp.add_parser("build"); b.add_argument("workdir"); b.add_argument("--entry", default="index.html")
    b.add_argument("--out", required=True); b.add_argument("--title", default="Untitled")
    b.add_argument("--source"); b.add_argument("--provenance"); b.add_argument("--seed")
    b.add_argument("--created", help="fixed timestamp for reproducible packing")
    b.add_argument("--license", default="token"); b.add_argument("--duration", type=int, default=300)
    b.add_argument("--viewport", default="1920x1080"); b.add_argument("--budget", type=int, default=10000)
    b.add_argument("--engine"); b.add_argument("--host", action="store_true")
    b.add_argument("--allow-render-errors", action="store_true"); b.set_defaults(f=cmd_build)
    v = sp.add_parser("verify"); v.add_argument("capsule"); v.add_argument("--png"); v.add_argument("--engine")
    v.add_argument("--host", action="store_true"); v.add_argument("--allow-engine-mismatch", action="store_true")
    v.add_argument("--sig", help="signature sidecar (default: <capsule>.sig.json)")
    v.add_argument("--container", action="store_true", help="already inside the engine image (see verify-in-docker.sh)")
    v.add_argument("--require-signer", metavar="DID", help="fail unless a valid signature by this did:key is present")
    v.set_defaults(f=cmd_verify)
    k = sp.add_parser("keygen"); k.add_argument("key"); k.set_defaults(f=cmd_keygen)
    g = sp.add_parser("sign"); g.add_argument("capsule"); g.add_argument("--key", required=True)
    g.add_argument("--role", default="publisher"); g.set_defaults(f=cmd_sign)
    i = sp.add_parser("info"); i.add_argument("capsule"); i.set_defaults(f=cmd_info)
    a = p.parse_args(); a.f(a)

if __name__ == "__main__":
    main()
