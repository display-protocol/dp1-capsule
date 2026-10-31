#!/usr/bin/env python3
"""dp1c: build and verify DP-1 preservation capsules (*.dp1c).

Prototype. Fills the lines DP-1 v1.1.0 leaves blank:
  §9  capsule layout      -> deterministic tar+zstd, sha256 = capsule id
  /meta/engine/<layer>    -> a pinned engine description with a content-addressed imageRef
  §5  repro.frameHash     -> computed by harness.py over the DevTools protocol, inside the pinned engine,
                             with the network unshared, the clock pinned, the viewport set before navigation

Commands
  build           WORKDIR --engine ENGINE --out X.dp1c [--entry index.html] [--title ..] [--source URL]
                  [--provenance p.json] [--seed 0x..] [--created ISO] [--host]
  import-capture  --cache-root DIR --source URL --engine ENGINE --out X.dp1c
                  (a capture FF OS already made: items/<sha256(source)>.json + blobs/; the capsule replays it)
  verify          X.dp1c [--engine ENGINE] [--host] [--png out.png]
  info            X.dp1c

ENGINE is an engine-<sha>.tar.zst from engine/build-engine.sh, a bare sha already in the cache, or a rootfs
directory (unpinned: reported as a different engine). --host runs this machine's chromium outside the engine
sandbox; it is a weaker claim and is always reported as such.
Only stdlib + zstandard + Pillow + bubblewrap. No Feral File service anywhere.
"""

import argparse, base64, hashlib, io, json, os, platform, re, shlex, shutil, subprocess, sys, tarfile, tempfile, uuid
from datetime import datetime, timezone

import zstandard
from PIL import Image

DP_VERSION = "1.1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
HARNESS = os.path.join(HERE, "harness.py")
CHROMIUM = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
ENGINE_CACHE = os.path.expanduser("~/.cache/dp1c/engines")
MAX_CAPSULE_BYTES = 2 * 1024**3
MAX_MEMBERS = 50000
MAX_BODY = 200 * 1024 * 1024  # largest response body a replay capsule may carry (one DevTools message each)
HEX64 = re.compile(r"^[0-9a-f]{64}$")
REPLAY_HEADERS = {
    "access-control-allow-origin",
    "access-control-allow-credentials",
    "access-control-allow-methods",
    "access-control-allow-headers",
    "access-control-expose-headers",
    "cross-origin-resource-policy",
    "cross-origin-embedder-policy",
    "timing-allow-origin",
    "content-type",
}
VIRTUAL_EPOCH = 1_600_000_000  # 2020-09-13T12:26:40Z: the clock every render starts from

# ---------- the engine pin ---------------------------------------------------

DETERMINISTIC_FLAGS = [
    "--headless=new",
    "--disable-gpu",  # software raster: pixels do not depend on a GPU or driver
    "--use-angle=swiftshader",  # and name the software backend instead of relying on the fallback
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
    "--no-sandbox",  # the sandbox is bubblewrap's, outside the browser
    "--user-data-dir=/tmp/profile",
]
SANDBOX_ENV = {
    "PATH": "/usr/bin:/bin",
    "HOME": "/tmp",
    "TMPDIR": "/tmp",
    "XDG_CONFIG_HOME": "/tmp/xdg",
    "XDG_CACHE_HOME": "/tmp/xdgc",
    "XDG_RUNTIME_DIR": "/tmp/run",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "TZ": "UTC",
}


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canon(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def engine_pin(version, viewport, budget_ms, image_ref, random_seed=None, frames=60, frame_interval_ms=16.667):
    return {
        "engine": "chromium",
        "version": version,
        "platform": "linux-x86_64",
        "renderer": "software",
        "flags": list(DETERMINISTIC_FLAGS),
        "env": dict(SANDBOX_ENV),
        "viewport": {"width": viewport[0], "height": viewport[1], "deviceScaleFactor": 1},
        "virtualEpochSeconds": VIRTUAL_EPOCH,
        "virtualTimeBudgetMs": budget_ms,
        "timezone": "UTC",
        "locale": "en-US",
        # the frame schedule: after the settle budget, exactly this many animation frames at this interval, then freeze
        "frames": frames,
        "frameIntervalMs": frame_interval_ms,
        "hardwareConcurrency": 4,
        "loopbackPort": 8371,  # files layout: the origin the page sees; fixed so two builds record the same href (the network namespace is private)
        "randomSeed": random_seed,  # None: the work's own randomness is left alone (on-chain gen art seeds itself)
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
        side = spec[: -len(".tar.zst")] + ".json" if spec.endswith(".tar.zst") else None
        man = json.load(open(side)) if side and os.path.exists(side) else None
        root = _cache_root(sha)
        if root is None:
            final = os.path.join(ENGINE_CACHE, sha)
            tmp = final + ".partial"
            _rmtree(tmp)
            _rmtree(final)  # an incomplete entry is garbage
            os.makedirs(os.path.join(tmp, "rootfs"))
            subprocess.run(
                f"zstd -d -q -c {shlex.quote(spec)} | tar -x -C {shlex.quote(os.path.join(tmp, 'rootfs'))}",
                shell=True,
                check=True,
            )
            if man:
                json.dump(man, open(os.path.join(tmp, "engine.json"), "w"), indent=2, sort_keys=True)
            open(os.path.join(tmp, "COMPLETE"), "w").write(sha + "\n")
            os.replace(tmp, final)
            root = os.path.join(final, "rootfs")
        return _mountpoints(root), sha, man or _cache_manifest(sha)
    root = _cache_root(spec)
    if root:
        return _mountpoints(root), spec, _cache_manifest(spec)
    sys.exit(f"engine {spec} not found (not a dir, not a tar.zst, not a completed entry in {ENGINE_CACHE})")


def _rmtree(path):
    """rmtree that first makes every directory writable (a rootfs has 0555 directories)."""
    if not os.path.lexists(path):
        return
    for dirpath, dirs, _ in os.walk(path):
        for d in dirs:
            p = os.path.join(dirpath, d)
            if not os.path.islink(p):
                try:
                    os.chmod(p, 0o700)
                except OSError:
                    pass
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
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
    for m in ("dev", "proc", "tmp", "work", "out", "harness"):
        os.makedirs(os.path.join(root, m), exist_ok=True)
    return root


def _env_args():
    out = ["--clearenv"]
    for k, v in SANDBOX_ENV.items():
        out += ["--setenv", k, v]
    return out


def engine_version(root):
    out = subprocess.run(
        [
            "bwrap",
            "--ro-bind",
            root,
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
            "--tmpfs",
            "/tmp",
            "--unshare-all",
            *_env_args(),
            "/usr/bin/chromium",
            "--version",
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    return _ver(out)


def host_version():
    if not CHROMIUM:
        sys.exit("--host asked for but no chromium binary on PATH")
    return _ver(subprocess.run([CHROMIUM, "--version"], capture_output=True, text=True).stdout)


def _ver(s):
    for tok in s.split():
        if tok[0].isdigit() and tok.count(".") == 3:
            return tok
    return s.strip()


# ---------- render ----------------------------------------------------------


def render(root, assets_dir, entry, engine, outdir, replay=None):
    """Run harness.py under bubblewrap with the network namespace unshared. root is the pinned engine rootfs, or None
    for the host's own filesystem and chromium (--host: a different-engine comparison, never 'reproduced').
    replay: {"entry": url, "resources": [...]} for a captured work (assets are blobs named by sha256; the harness answers
    the page's requests from the table). Writes shot.png + render.json into outdir, a fresh directory outside the capsule."""
    for f in ("shot.png", "render.json"):
        if os.path.exists(os.path.join(outdir, f)):
            os.remove(os.path.join(outdir, f))
    hdir = os.path.join(outdir, "_harness")
    os.makedirs(hdir, exist_ok=True)
    shutil.copy(HARNESS, os.path.join(hdir, "harness.py"))
    if root == "CONTAINER":
        # we are already inside the engine image (verify --container); the container runtime isolates the network
        cfg = {
            "out": outdir,
            "work": os.path.abspath(assets_dir),
            "entry": entry,
            "chromium": "/usr/bin/chromium",
            "flags": engine["flags"],
            "viewport": engine["viewport"],
            "virtualEpochSeconds": engine["virtualEpochSeconds"],
            "virtualTimeBudgetMs": engine["virtualTimeBudgetMs"],
            "timezone": engine["timezone"],
            "locale": engine["locale"],
            "randomSeed": engine.get("randomSeed"),
            "frames": engine.get("frames"),
            "frameIntervalMs": engine.get("frameIntervalMs"),
            "hardwareConcurrency": engine.get("hardwareConcurrency"),
            "loopbackPort": engine.get("loopbackPort"),
            "cwd": "/tmp",
            "chromiumPrefix": [],
            "replay": replay,
            "wallTimeoutSeconds": int(os.environ.get("DP1C_WALL_SECONDS", "900")),
            "debug": bool(os.environ.get("DP1C_DEBUG")),
        }
        env = dict(SANDBOX_ENV)
        os.makedirs("/tmp/xdg", exist_ok=True)
        os.makedirs("/tmp/xdgc", exist_ok=True)
        os.makedirs("/tmp/run", exist_ok=True)
        os.makedirs("/tmp/profile", exist_ok=True)
        r = subprocess.run(
            [sys.executable, HARNESS], input=canon(cfg), capture_output=True, text=True, timeout=1800, env=env
        )
        shot = os.path.join(outdir, "shot.png")
        rj = os.path.join(outdir, "render.json")
        if r.returncode != 0 or not os.path.isfile(shot) or not os.path.isfile(rj):
            sys.exit(
                f"render failed (exit {r.returncode}):\n{r.stderr if os.environ.get('DP1C_DEBUG') else r.stderr[-3000:]}"
            )
        return open(shot, "rb").read(), json.load(open(rj))
    if root:
        fs = ["--ro-bind", root, "/"]
        W, O, H = "/work", "/out", "/harness"
        chromium, python = "/usr/bin/chromium", "/usr/bin/python"
    else:
        if not CHROMIUM:
            sys.exit("--host asked for but no chromium binary on PATH")
        fs = [
            "--ro-bind",
            "/",
            "/",
            "--tmpfs",
            "/mnt",
            "--dir",
            "/mnt/work",
            "--dir",
            "/mnt/out",
            "--dir",
            "/mnt/harness",
        ]
        W, O, H = "/mnt/work", "/mnt/out", "/mnt/harness"
        chromium, python = CHROMIUM, sys.executable
    setpriv = os.path.join(root or "", "usr/bin/setpriv") if root else shutil.which("setpriv")
    cfg = {
        "out": O,
        "work": W,
        "entry": entry,
        "chromium": chromium,
        "flags": engine["flags"],
        "viewport": engine["viewport"],
        "virtualEpochSeconds": engine["virtualEpochSeconds"],
        "virtualTimeBudgetMs": engine["virtualTimeBudgetMs"],
        "timezone": engine["timezone"],
        "locale": engine["locale"],
        "randomSeed": engine.get("randomSeed"),
        "frames": engine.get("frames"),
        "frameIntervalMs": engine.get("frameIntervalMs"),
        "hardwareConcurrency": engine.get("hardwareConcurrency"),
        "loopbackPort": engine.get("loopbackPort"),
        "cwd": "/tmp",
        "replay": replay,
        "wallTimeoutSeconds": int(os.environ.get("DP1C_WALL_SECONDS", "900")),
        "debug": bool(os.environ.get("DP1C_DEBUG")),
        # after loopback is up the harness launches chromium with every capability dropped
        "chromiumPrefix": ["/usr/bin/setpriv", "--inh-caps=-all", "--bounding-set=-all", "--no-new-privs"]
        if (setpriv and os.path.exists(setpriv))
        else [],
    }
    cmd = [
        "bwrap",
        *fs,
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--tmpfs",
        "/tmp",
        "--ro-bind",
        os.path.abspath(assets_dir),
        W,
        "--bind",
        os.path.abspath(outdir),
        O,
        "--ro-bind",
        hdir,
        H,
        "--unshare-all",
        "--uid",
        "0",
        "--gid",
        "0",
        "--cap-add",
        "CAP_NET_ADMIN",
        "--cap-add",
        "CAP_SETPCAP",
        "--chdir",
        "/",
        "--die-with-parent",
        *_env_args(),
        "/bin/bash",
        "-c",
        f"mkdir -p /tmp/xdg /tmp/xdgc /tmp/run /tmp/profile && exec {shlex.quote(python)} {H}/harness.py",
    ]
    if os.environ.get("DP1C_DEBUG"):
        print("bwrap:", " ".join(shlex.quote(c) for c in cmd), file=sys.stderr)
    r = subprocess.run(cmd, input=canon(cfg), capture_output=True, text=True, timeout=1800)
    shot = os.path.join(outdir, "shot.png")
    rj = os.path.join(outdir, "render.json")
    if r.returncode != 0 or not os.path.isfile(shot) or not os.path.isfile(rj):
        sys.exit(
            f"render failed (exit {r.returncode}):\n{r.stderr if os.environ.get('DP1C_DEBUG') else r.stderr[-3000:]}"
        )
    return open(shot, "rb").read(), json.load(open(rj))


def render_problems(rj):
    d = rj["diagnostics"]
    http = [e for e in d["httpErrors"] if not e["url"].endswith("/favicon.ico")]
    misses = [m for m in d.get("replayMisses", []) if not m.endswith("/favicon.ico")]
    fails = [e for e in d["loadFailures"] if not (e.get("url") or "").endswith("/favicon.ico")]
    other = [t for t in d.get("otherTargets", []) if t.get("type") not in ("page", "browser")]
    return {
        "loadFailures": fails,
        "httpErrors": http,
        "exceptions": d["exceptions"],
        "replayMisses": misses,
        "inflightAtCapture": d.get("inflightAtCapture", []),
        "outsideEngineClock": other,
    }  # workers / out-of-process frames: not ticked, not intercepted


def replay_problems(rj, capture):
    """Replay-specific completeness: the main document must have come from the table and the page must have landed on
    a captured URL. Without this a capsule could name a work, replay nothing, and match a reference of nothing."""
    out = []
    d, page = rj["diagnostics"], rj.get("page", {})
    if not d.get("entryFulfilled"):
        out.append("the entry document was not served from the capture")
    urls = {r["url"] for r in capture["resources"]}
    if page.get("href") not in urls:
        out.append(f"page landed on {page.get('href')}, not a captured URL")
    return out


def check_capture(capture, manifest):
    """Static checks on a replay table against the asset manifest, done before anything is rendered."""
    bad = []
    e = capture.get("entry") or ""
    if not (e.startswith("https://") or e.startswith("http://")):
        bad.append("capture entry is not an http(s) URL")
    seen = set()
    for r in capture.get("resources", []):
        key = (r.get("method") or "GET").upper() + " " + str(r.get("url"))
        if key in seen:
            bad.append("duplicate resource " + key)
        seen.add(key)
        if (r.get("method") or "GET").upper() not in ("GET", "HEAD"):
            bad.append("unsupported method in " + key)
        if not isinstance(r.get("status"), int) or not (100 <= r["status"] <= 599):
            bad.append("bad status in " + key)
        if r.get("redirectTo") and not (
            isinstance(r["status"], int)
            and 300 <= r["status"] <= 399
            and str(r["redirectTo"]).startswith(("http://", "https://"))
        ):
            bad.append("redirect record without a 3xx status and an http(s) target: " + key)
        if r.get("redirectTo") and r.get("sha256"):
            bad.append("record has both a redirect and a body: " + key)
        for h in r.get("headers") or {}:
            if not isinstance(h, str) or h.lower() not in REPLAY_HEADERS:
                bad.append(f"header {h!r} is not replayable: " + key)
        sha = r.get("sha256")
        if sha is not None:
            if not isinstance(sha, str) or not HEX64.match(sha):
                bad.append("bad digest in " + key)
            elif manifest is not None and manifest.get(sha) != sha:
                bad.append(f"resource {r.get('url')} names a body that is not assets/{sha}")
    if ("GET " + e) not in seen:
        bad.append("capture has no GET record for its entry")
    if manifest is not None:
        used = {r.get("sha256") for r in capture.get("resources", [])}
        bad += [f"assets/{k} is not referenced by any captured resource" for k in sorted(manifest) if k not in used]
    return bad


def check_pin(engine):
    """The engine layer is capsule data; only sane values reach the harness."""
    bad = []
    vp = engine.get("viewport") or {}
    if not all(isinstance(vp.get(k), int) and 1 <= vp[k] <= 16384 for k in ("width", "height")):
        bad.append("viewport out of range")
    if vp.get("deviceScaleFactor") != 1:
        bad.append("deviceScaleFactor must be 1")
    f = engine.get("frames", 0)
    iv = engine.get("frameIntervalMs", 16.667)
    if not isinstance(f, int) or not (0 <= f <= 100000):
        bad.append("frames out of range")
    if not isinstance(iv, (int, float)) or not (1 <= iv <= 1000):
        bad.append("frameIntervalMs out of range")
    b = engine.get("virtualTimeBudgetMs")
    if not isinstance(b, int) or not (0 <= b <= 3600000):
        bad.append("virtualTimeBudgetMs out of range")
    if engine.get("randomSeed") is not None and not isinstance(engine["randomSeed"], int):
        bad.append("randomSeed must be an integer")
    return bad


def dhash(im, size=8):
    g = im.convert("L").resize((size + 1, size), Image.LANCZOS)
    px = list(g.get_flattened_data()) if hasattr(g, "get_flattened_data") else list(g.getdata())
    bits = "".join(
        "1" if px[r * (size + 1) + c] > px[r * (size + 1) + c + 1] else "0" for r in range(size) for c in range(size)
    )
    return "0x" + int(bits, 2).to_bytes(size * size // 8, "big").hex()


def frame_hash(png_bytes, viewport):
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    if im.size != (viewport["width"], viewport["height"]):
        sys.exit(f"captured frame is {im.size}, pin says {viewport['width']}x{viewport['height']}")
    h = hashlib.sha256(f"{im.width}x{im.height}:RGB8:".encode())
    h.update(im.tobytes())
    return {"sha256": h.hexdigest(), "phash": dhash(im), "width": im.width, "height": im.height, "format": "RGB8"}


# ---------- archive ---------------------------------------------------------


def walk_files(root):
    out = []
    for dirpath, _, files in os.walk(root):
        for n in files:
            out.append(os.path.relpath(os.path.join(dirpath, n), root))
    return sorted(out)


def pack(stage_dir, out_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w", format=tarfile.USTAR_FORMAT) as tar:
        for arc in walk_files(stage_dir):
            full = os.path.join(stage_dir, arc)
            ti = tar.gettarinfo(full, arcname=arc)
            ti.mtime = 0
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            ti.mode = 0o644
            with open(full, "rb") as f:
                tar.addfile(ti, f)
    data = zstandard.ZstdCompressor(level=19, write_checksum=False, write_content_size=True).compress(buf.getvalue())
    with open(out_path, "wb") as f:
        f.write(data)
    return hashlib.sha256(data).hexdigest()


def unpack(capsule_path, dest):
    if os.path.getsize(capsule_path) > MAX_CAPSULE_BYTES:
        sys.exit("capsule too large")
    with open(capsule_path, "rb") as f:
        raw = zstandard.ZstdDecompressor(max_window_size=2**31).stream_reader(f).read(MAX_CAPSULE_BYTES + 1)
    if len(raw) > MAX_CAPSULE_BYTES:
        sys.exit("capsule decompresses beyond the limit")
    with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
        members = tar.getmembers()
        if len(members) > MAX_MEMBERS:
            sys.exit("too many members")
        for m in members:
            if not (m.isreg() or m.isdir()):
                sys.exit(f"capsule may only contain files and directories: {m.name}")
            if m.name.startswith("/") or ".." in m.name.split("/"):
                sys.exit(f"unsafe entry {m.name}")
        tar.extractall(dest, filter="data")


def check_assets(assets_dir, manifest):
    """Every regular file under assets/ must be in the manifest with a matching hash, and vice versa."""
    present = walk_files(assets_dir)
    for k in manifest:
        if k.startswith("/") or ".." in k.split("/"):
            return ["unsafe manifest key " + k]
    bad = [
        k
        for k in sorted(set(present) | set(manifest))
        if k not in manifest or k not in present or sha256_file(os.path.join(assets_dir, k)) != manifest[k]
    ]
    return bad


# ---------- whole-capsule signature (DP-1 §7 object shape, over the capsule id) ----------

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(b):
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = B58[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


def b58decode(s):
    n = 0
    for ch in s:
        n = n * 58 + B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(s) - len(s.lstrip("1"))) + raw


def did_key_from_pub(pub_bytes):
    return "did:key:z" + b58encode(b"\xed\x01" + pub_bytes)  # multicodec ed25519-pub


def pub_from_did_key(did):
    if not did.startswith("did:key:z"):
        raise ValueError("only did:key with base58btc is supported")
    raw = b58decode(did[len("did:key:z") :])
    if raw[:2] != b"\xed\x01" or len(raw) != 34:
        raise ValueError("not an ed25519 did:key")
    return raw[2:]


def sig_sidecar(capsule_path):
    return capsule_path + ".sig.json"


def b64u(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64u_dec(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def cmd_keygen(a):
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization

    if os.path.exists(a.key):
        sys.exit(f"{a.key} exists; not overwriting a key")
    k = ed25519.Ed25519PrivateKey.generate()
    pem = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    fd = os.open(a.key, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.write(fd, pem)
    os.close(fd)
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    print(json.dumps({"key": a.key, "kid": did_key_from_pub(pub)}, indent=2))


def cmd_sign(a):
    from cryptography.hazmat.primitives.asymmetric import ed25519
    from cryptography.hazmat.primitives import serialization

    k = serialization.load_pem_private_key(open(a.key, "rb").read(), password=None)
    if not isinstance(k, ed25519.Ed25519PrivateKey):
        sys.exit("key is not ed25519")
    pub = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    cid = sha256_file(a.capsule)
    payload_hash = "sha256:" + cid
    entry = {
        "alg": "ed25519",
        "kid": did_key_from_pub(pub),
        "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "payload_hash": payload_hash,
        "role": a.role,
        "sig": b64u(k.sign(payload_hash.encode())),
    }
    side = sig_sidecar(a.capsule)
    doc = json.load(open(side)) if os.path.exists(side) else {"capsuleId": cid, "signatures": []}
    if doc.get("capsuleId") != cid:
        sys.exit(f"{side} belongs to a different capsule ({doc.get('capsuleId')})")
    doc["signatures"] = [x for x in doc["signatures"] if x["kid"] != entry["kid"]] + [entry]
    open(side, "w").write(canon(doc))
    print(json.dumps({"capsuleId": cid, "sidecar": side, "kid": entry["kid"], "role": a.role}, indent=2))


def _ed25519_verify_pure(pub, sig, msg):
    """RFC 8032 Ed25519 verification in plain Python, so an engine image without a signature library can still check a
    sidecar. Tens of milliseconds per signature; no secrets are handled here, so timing is not a concern."""
    p = 2**255 - 19
    L = 2**252 + 27742317777372353535851937790883648493
    d = (-121665 * pow(121666, p - 2, p)) % p

    def add(P, Q):
        X1, Y1, Z1, T1 = P
        X2, Y2, Z2, T2 = Q
        A = (Y1 - X1) * (Y2 - X2) % p
        B = (Y1 + X1) * (Y2 + X2) % p
        C = 2 * T1 * T2 * d % p
        D = 2 * Z1 * Z2 % p
        E, F, G, H = B - A, D - C, D + C, B + A
        return (E * F % p, G * H % p, F * G % p, E * H % p)

    def mul(s, P):
        Q = (0, 1, 1, 0)
        while s:
            if s & 1:
                Q = add(Q, P)
            P = add(P, P)
            s >>= 1
        return Q

    def decode(b):
        if len(b) != 32:
            return None
        y = int.from_bytes(b, "little")
        sign = y >> 255
        y &= (1 << 255) - 1
        if y >= p:
            return None
        x2 = (y * y - 1) * pow(d * y * y + 1, p - 2, p) % p
        x = pow(x2, (p + 3) // 8, p)
        if (x * x - x2) % p:
            x = x * pow(2, (p - 1) // 4, p) % p
        if (x * x - x2) % p:
            return None
        if x == 0 and sign:
            return None
        if (x & 1) != sign:
            x = p - x
        return (x, y, 1, x * y % p)

    def same(P, Q):
        return (P[0] * Q[2] - Q[0] * P[2]) % p == 0 and (P[1] * Q[2] - Q[1] * P[2]) % p == 0

    G = (
        15112221349535400772501151409588531511454012693041857206046113283949847762202,
        46316835694926478169428394003475163141307993866256225615783033603165251855960,
        1,
        0,
    )
    G = (G[0], G[1], 1, G[0] * G[1] % p)
    if len(sig) != 64:
        return False
    A, R = decode(pub), decode(sig[:32])
    s = int.from_bytes(sig[32:], "little")
    if A is None or R is None or s >= L:
        return False
    k = int.from_bytes(hashlib.sha512(sig[:32] + pub + msg).digest(), "little") % L
    return same(mul(s, G), add(R, mul(k, A)))


def check_signatures(capsule_path, sig_path=None):
    """Return (list of {kid, role, ts, valid, reason}) for the sidecar, or [] if there is none."""
    side = sig_path or sig_sidecar(capsule_path)
    if not os.path.exists(side):
        return []
    cid = sha256_file(capsule_path)
    doc = json.load(open(side))
    out = []
    try:
        from cryptography.hazmat.primitives.asymmetric import ed25519
        from cryptography.exceptions import InvalidSignature

        def verify(pub, sig, msg):
            ed25519.Ed25519PublicKey.from_public_bytes(pub).verify(sig, msg)

        checked_with = "cryptography"
    except ImportError:
        # the engine image carries no signature library; RFC 8032 in plain Python does the same check
        InvalidSignature = ValueError

        def verify(pub, sig, msg):
            if not _ed25519_verify_pure(pub, sig, msg):
                raise ValueError("signature does not verify")

        checked_with = "pure-python"
    for x in doc.get("signatures", []):
        r = {
            "kid": x.get("kid"),
            "role": x.get("role"),
            "ts": x.get("ts"),
            "valid": False,
            "reason": "",
            "checkedWith": checked_with,
        }
        try:
            if doc.get("capsuleId") != cid or x.get("payload_hash") != "sha256:" + cid:
                raise ValueError("payload_hash is not this capsule's id")
            if x.get("alg") != "ed25519":
                raise ValueError("unsupported alg")
            verify(pub_from_did_key(x["kid"]), b64u_dec(x["sig"]), x["payload_hash"].encode())
            r["valid"] = True
        except (InvalidSignature, ValueError, KeyError) as e:
            r["reason"] = str(e) or type(e).__name__
        out.append(r)
    return out


# ---------- commands --------------------------------------------------------


def open_engine_for_build(a):
    viewport = tuple(int(x) for x in a.viewport.split("x"))
    if a.engine:
        root, sha, man = engine_open(a.engine)
        image_ref = (man or {}).get("imageRef") or {}
        image_ref = {**image_ref, "sha256": sha}  # the computed digest is authoritative; the sidecar only adds packages
        version = engine_version(root)
    elif a.host:
        root, version, image_ref = None, host_version(), None
    else:
        sys.exit("build needs --engine (pinned) or --host (unpinned, weaker claim)")
    engine = engine_pin(version, viewport, a.budget, image_ref, a.pin_random, a.frames, a.frame_interval)
    bad = check_pin(engine)
    if bad:
        sys.exit("bad render settings: " + "; ".join(bad))
    return root, version, image_ref, engine


def write_capsule(a, stage, outdir, root, version, image_ref, engine, entry, item_base, source, capture=None):
    """Render the staged assets/ and finish the capsule. entry is a file name (files mode) or the work's URL (replay mode,
    when capture is the resources record). item_base is the DP-1 item to carry (verbatim from a device record, or built
    from the build flags); repro and a content-derived id are added here."""
    assets = os.path.join(stage, "assets")
    asset_hashes = {k: sha256_file(os.path.join(assets, k)) for k in walk_files(assets)}
    replay = {"entry": entry, "resources": capture["resources"]} if capture else None
    png, rj = render(root, assets, entry, engine, outdir, replay)
    problems = render_problems(rj)
    if capture:
        problems["replay"] = replay_problems(rj, capture)
    if any(problems.values()) and not a.allow_render_errors:
        sys.exit(
            "the work did not render cleanly; capsule not written:\n"
            + canon(problems)
            + "\n(pass --allow-render-errors to record them in the capsule anyway)"
        )
    fh = frame_hash(png, engine["viewport"])
    now = a.created or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    fingerprint = source + "|" + "|".join(f"{k}={asset_hashes[k]}" for k in sorted(asset_hashes))
    item = {
        **item_base,
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "dp1c:item:" + fingerprint)),
        "source": source,
        "repro": {
            "engineVersion": {"chromium": version},
            **({"seed": a.seed} if a.seed else {}),
            "assetsSHA256": [asset_hashes[k] for k in sorted(asset_hashes)],
            "frameHash": {"sha256": fh["sha256"], "phash": fh["phash"]},
        },
    }
    if a.provenance:
        item["provenance"] = json.load(open(a.provenance))
    playlist = {
        "dpVersion": DP_VERSION,
        "id": str(uuid.uuid5(uuid.NAMESPACE_URL, "dp1c:playlist:" + fingerprint)),
        "title": item.get("title") or a.title,
        "created": now,
        "defaults": {"display": {"scaling": "fit", "background": "#000000"}},
        "items": [item],
    }
    os.makedirs(os.path.join(stage, "meta", "engine"))
    os.makedirs(os.path.join(stage, "meta", "render"))
    open(os.path.join(stage, "playlist.json"), "w").write(canon(playlist))
    open(os.path.join(stage, "meta", "engine", "chromium.json"), "w").write(canon(engine))
    if capture:
        os.makedirs(os.path.join(stage, "meta", "capture"))
        open(os.path.join(stage, "meta", "capture", "resources.json"), "w").write(canon(capture))
    open(os.path.join(stage, "meta", "render", "reference.json"), "w").write(
        canon(
            {
                "mode": "replay" if capture else "files",
                "entry": entry,
                "assets": asset_hashes,
                "renderedAt": now,
                "frame": fh,
                "page": rj["page"],
                "glRenderer": rj.get("glRenderer"),
                "problems": problems,
                "png": "meta/render/reference.png",
                # what the frame clock did before the capture: ticks delivered, animation callbacks that ran, documents ticked
                "frameClock": {
                    "ticked": rj["diagnostics"].get("framesTicked"),
                    "callbacks": rj["diagnostics"].get("rafCallbacks"),
                    "contexts": rj["diagnostics"].get("frameContexts"),
                },
                **({"capture": "meta/capture/resources.json"} if capture else {}),
            }
        )
    )
    shutil.copy(os.path.join(outdir, "shot.png"), os.path.join(stage, "meta", "render", "reference.png"))
    cid = pack(stage, a.out)
    print(
        json.dumps(
            {
                "capsule": a.out,
                "capsuleId": cid,
                "frameHash": fh["sha256"],
                "phash": fh["phash"],
                "engine": version,
                "engineImage": (image_ref or {}).get("sha256"),
                "mode": "replay" if capture else "files",
                "ranInsideEngine": root is not None,
                "problems": problems,
                "assets": len(asset_hashes),
                **({"replayed": rj["diagnostics"].get("replayed")} if capture else {}),
            },
            indent=2,
        )
    )


def cmd_build(a):
    work = os.path.abspath(a.workdir)
    # the entry is a file name, optionally with a query the work reads (fxhash-style seeding: index.html?fxhash=0x...)
    entry_path, _, entry_query = a.entry.partition("?")
    if not os.path.isfile(os.path.join(work, entry_path)):
        sys.exit(f"entry {entry_path} not in {work}")
    if "/" in entry_path or entry_path.startswith(".") or "#" in a.entry:
        sys.exit("entry must be a plain file name at the asset root, with at most a query string")
    root, version, image_ref, engine = open_engine_for_build(a)
    with tempfile.TemporaryDirectory() as stage, tempfile.TemporaryDirectory() as outdir:
        shutil.copytree(work, os.path.join(stage, "assets"))
        item_base = {"title": a.title, "duration": a.duration, "license": a.license}
        write_capsule(
            a, stage, outdir, root, version, image_ref, engine, a.entry, item_base, a.source or f"dp1c:assets/{a.entry}"
        )


def load_capture_record(path):
    """An FF OS offline-cache item record (items/<sha256(source)>.json), with or without the automatic-cache wrapper."""
    rec = json.load(open(path))
    if "record" in rec and "automaticCacheVersion" in rec:
        rec = rec["record"]
    for k in ("item", "entry", "resources"):
        if k not in rec:
            sys.exit(f"{path}: not an offline-cache item record (missing {k})")
    return rec


def cmd_import_capture(a):
    """Turn a capture the device already made (FF OS feral-controld offlinecache store) into a capsule.
    Layout read: <root>/items/<sha256(source)>.json + <root>/blobs/<sha256>. Nothing in the store is modified."""
    if a.record:
        rec_path, blobs = a.record, a.blobs or os.path.join(os.path.dirname(os.path.dirname(a.record)), "blobs")
    else:
        if not (a.cache_root and a.source):
            sys.exit("import-capture needs --cache-root and --source, or --record (and --blobs)")
        rec_path = os.path.join(a.cache_root, "items", hashlib.sha256(a.source.encode()).hexdigest() + ".json")
        blobs = os.path.join(a.cache_root, "blobs")
        if not os.path.isfile(rec_path):
            sys.exit(f"no capture for {a.source} in {a.cache_root} (expected {rec_path})")
    rec = load_capture_record(rec_path)
    source = rec["item"].get("source")
    if a.source and source != a.source:
        sys.exit(f"record is for {source}, not {a.source}")
    if not rec.get("coverage", {}).get("complete") and not a.allow_incomplete:
        sys.exit(
            "the device marked this capture incomplete ("
            + str(rec.get("coverage", {}).get("reason"))
            + "); pass --allow-incomplete to build anyway"
        )
    resources = []
    for r in rec["resources"]:
        keep = {
            k: r[k]
            for k in ("url", "status", "sha256", "contentType", "sniffedContentType", "redirectTo", "method")
            if r.get(k)
        }
        hdrs = {k: v for k, v in (r.get("headers") or {}).items() if isinstance(k, str) and k.lower() in REPLAY_HEADERS}
        if hdrs:
            keep["headers"] = hdrs
        if "status" not in keep:
            keep["status"] = r.get("status", 0)
        resources.append(keep)
    capture = {
        "entry": rec["entry"],
        "capturedAt": rec.get("capturedAt"),
        "coverage": rec.get("coverage"),
        "capturedBy": a.captured_by,
        "resources": resources,
    }
    bad = check_capture(capture, None)
    if bad:
        sys.exit(
            "this capture cannot be a capsule (prototype limits: GET/HEAD only, one record per method+URL, bodies under %d MB):\n  "
            % (MAX_BODY >> 20)
            + "\n  ".join(bad)
        )
    root, version, image_ref, engine = open_engine_for_build(a)
    with tempfile.TemporaryDirectory() as stage, tempfile.TemporaryDirectory() as outdir:
        assets = os.path.join(stage, "assets")
        os.makedirs(assets)
        for sha in sorted({r["sha256"] for r in resources if r.get("sha256")}):
            src = os.path.join(blobs, sha)
            if not os.path.isfile(src):
                sys.exit(f"blob {sha} missing from {blobs}")
            if os.path.getsize(src) > MAX_BODY:
                sys.exit(
                    f"blob {sha} is {os.path.getsize(src) >> 20} MB; bodies over {MAX_BODY >> 20} MB are not supported in this prototype"
                )
            if sha256_file(src) != sha:
                sys.exit(f"blob {sha} does not hash to its name; the store is corrupt")
            shutil.copy(src, os.path.join(assets, sha))
        item_base = {k: v for k, v in rec["item"].items() if k not in ("id", "repro")}
        item_base.setdefault("title", a.title)
        item_base.setdefault("duration", a.duration)
        item_base.setdefault("license", a.license)
        write_capsule(a, stage, outdir, root, version, image_ref, engine, rec["entry"], item_base, source, capture)


def cmd_verify(a):
    with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as outdir:
        unpack(a.capsule, d)
        pl = json.load(open(os.path.join(d, "playlist.json")))
        engine = json.load(open(os.path.join(d, "meta", "engine", "chromium.json")))
        ref = json.load(open(os.path.join(d, "meta", "render", "reference.json")))
        if len(pl.get("items", [])) != 1:
            sys.exit("this verifier handles exactly one item per capsule")
        item = pl["items"][0]
        want = item["repro"]["frameHash"]
        # 1. assets: whole tree vs manifest vs playlist
        bad = check_assets(os.path.join(d, "assets"), ref["assets"])
        if sorted(ref["assets"].values()) != sorted(item["repro"]["assetsSHA256"]):
            bad.append("playlist repro.assetsSHA256 disagrees with reference.json")
        # the three copies of the reference frame must agree: playlist, reference.json, and the PNG itself
        ref_png = frame_hash(open(os.path.join(d, "meta", "render", "reference.png"), "rb").read(), engine["viewport"])
        if not (
            ref_png["sha256"] == ref["frame"]["sha256"] == want["sha256"]
            and ref_png["phash"] == ref["frame"]["phash"] == want["phash"]
        ):
            bad.append("reference frame records disagree (playlist / reference.json / reference.png)")
        bad += check_pin(engine)
        capture, replay = None, None
        if ref.get("mode") == "replay":
            capture = json.load(open(os.path.join(d, "meta", "capture", "resources.json")))
            if capture.get("entry") != ref["entry"]:
                bad.append("capture entry disagrees with reference.json")
            if item["source"] != capture.get("entry") and not any(
                r["url"] == item["source"] for r in capture["resources"]
            ):
                bad.append("playlist source is neither the capture entry nor a captured URL")
            bad += check_capture(capture, ref["assets"])
            replay = {"entry": capture["entry"], "resources": capture["resources"]}
        elif ref.get("mode", "files") != "files":
            bad.append("unknown mode " + str(ref.get("mode")))
        elif ref["entry"].partition("?")[0] not in ref["assets"]:
            bad.append("entry file is not in the asset manifest")
        if bad:
            # nothing from a capsule that fails its static checks is rendered
            print(
                json.dumps(
                    {
                        "capsuleId": sha256_file(a.capsule),
                        "assetsIntact": False,
                        "assetProblems": bad,
                        "verdict": "assetsCorrupt",
                    },
                    indent=2,
                )
            )
            sys.exit(2)
        # 2. engine: computed digest is the only thing trusted
        pinned_img = (engine.get("imageRef") or {}).get("sha256")
        root, here_img = None, None
        if a.container:
            here_img = os.environ.get("DP1C_CONTAINER_ENGINE_SHA")
            if not here_img:
                sys.exit("--container needs DP1C_CONTAINER_ENGINE_SHA (the launcher's digest of the imported image)")
            root = "CONTAINER"
        elif a.engine:
            root, here_img, _ = engine_open(a.engine)
        elif pinned_img and _cache_root(pinned_img):
            root, here_img = _cache_root(pinned_img), pinned_img
        elif not a.host:
            sys.exit(
                f"capsule is pinned to engine image {pinned_img}; it is not in {ENGINE_CACHE} and no --engine was given. "
                f"Pass --engine <engine-{pinned_img}.tar.zst>, or --host to compare against this machine's chromium (weaker claim)."
            )
        engine_match = bool(root) and here_img is not None and here_img == pinned_img
        if root and not engine_match and not a.allow_engine_mismatch:
            sys.exit(
                f"engine mismatch: capsule pinned to {pinned_img}, given {here_img or 'an unpinned directory'} "
                f"(pass --allow-engine-mismatch to run anyway; the result is then a different-engine comparison)"
            )
        harness_match = engine.get("harnessSha256") == sha256_file(HARNESS)
        # 3. render with the pin's settings; flags come from this tool, never from the capsule
        run_engine = {**engine, "flags": list(DETERMINISTIC_FLAGS), "env": dict(SANDBOX_ENV)}
        here_ver = (
            _ver(subprocess.run(["/usr/bin/chromium", "--version"], capture_output=True, text=True).stdout)
            if root == "CONTAINER"
            else (engine_version(root) if root else host_version())
        )
        png, rj = render(root, os.path.join(d, "assets"), ref["entry"], run_engine, outdir, replay)
        if a.png:
            shutil.copy(os.path.join(outdir, "shot.png"), a.png)
        fh = frame_hash(png, engine["viewport"])
        exact = fh["sha256"] == want["sha256"]
        ham = bin(int(fh["phash"], 16) ^ int(want["phash"], 16)).count("1")
        problems = render_problems(rj)
        if capture:
            problems["replay"] = replay_problems(rj, capture)
        # a render that failed to load something, threw, left requests in flight, ran code outside the engine's clock, or
        # (replay) did not come from the capture is not a reproduction, even if the pixels match. A capsule built with
        # --allow-render-errors carries its problems and is reported the same way: an acknowledged partial, never exit 0.
        clean = not any(problems.values())
        sigs = check_signatures(a.capsule, a.sig)
        signer_ok = True
        if a.require_signer:
            signer_ok = any(x["valid"] and x["kid"] == a.require_signer for x in sigs)
        if bad:
            verdict = "assetsCorrupt"
        elif not signer_ok:
            verdict = "unsigned" if not sigs else "signatureInvalid"
        elif not clean:
            verdict = "renderProblems"
        elif exact:
            if engine_match and harness_match:
                verdict = "reproduced"
            elif not pinned_img and harness_match and here_ver == engine["version"]:
                verdict = "reproducedUnpinned"  # the capsule names no image (built with --host): same Chromium version and harness, but nothing pinned
            else:
                verdict = "reproducedOnDifferentEngine"
        elif ham <= 4 and not engine_match:
            verdict = "nearOnDifferentEngine"
        else:
            verdict = "reproMismatch"
        print(
            json.dumps(
                {
                    "capsuleId": sha256_file(a.capsule),
                    "assetsIntact": not bad,
                    "assetProblems": bad,
                    "engineImagePinned": pinned_img,
                    "engineImageHere": here_img,
                    "engineMatch": engine_match,
                    "enginePinned": engine["version"],
                    "engineHere": here_ver,
                    "ranInsideEngine": root is not None,
                    "sandbox": "container" if root == "CONTAINER" else ("bwrap" if root else "bwrap-host"),
                    "harnessMatch": harness_match,
                    "frameHashExpected": want["sha256"],
                    "frameHashHere": fh["sha256"],
                    "exact": exact,
                    "phashDistance": ham,
                    "renderProblems": problems,
                    "replayed": rj["diagnostics"].get("replayed"),
                    "framesTicked": rj["diagnostics"].get("framesTicked"),
                    "rafCallbacks": rj["diagnostics"].get("rafCallbacks"),
                    "signatures": sigs,
                    "requiredSigner": a.require_signer,
                    "verdict": verdict,
                },
                indent=2,
            )
        )
        sys.exit(0 if verdict == "reproduced" else 2)


def cmd_info(a):
    with tempfile.TemporaryDirectory() as d:
        unpack(a.capsule, d)
        for p in ("playlist.json", "meta/engine/chromium.json", "meta/render/reference.json"):
            print(f"--- {p}")
            print(json.dumps(json.load(open(os.path.join(d, p))), indent=2, sort_keys=True))
        cp = os.path.join(d, "meta", "capture", "resources.json")
        if os.path.exists(cp):
            c = json.load(open(cp))
            print("--- meta/capture/resources.json")
            print(json.dumps({k: v for k, v in c.items() if k != "resources"}, indent=2, sort_keys=True))
            for r in c["resources"]:
                print(
                    f"  {r.get('method') or 'GET'} {r['status']} {r.get('contentType') or '-'} {r['url']}"
                    + (f" -> {r['redirectTo']}" if r.get("redirectTo") else "")
                )


def main():
    p = argparse.ArgumentParser(prog="dp1c")
    sp = p.add_subparsers(dest="cmd", required=True)

    def build_args(x):
        x.add_argument("--out", required=True)
        x.add_argument("--title", default="Untitled")
        x.add_argument("--provenance")
        x.add_argument("--seed")
        x.add_argument("--created", help="fixed timestamp for reproducible packing")
        x.add_argument("--license", default="token")
        x.add_argument("--duration", type=int, default=300)
        x.add_argument("--viewport", default="1920x1080")
        x.add_argument("--budget", type=int, default=10000, help="settle budget in virtual ms before the first frame")
        x.add_argument(
            "--frames", type=int, default=60, help="animation frames to run before the capture (the frame schedule)"
        )
        x.add_argument("--frame-interval", type=float, default=16.667, metavar="MS")
        x.add_argument("--engine")
        x.add_argument("--host", action="store_true")
        x.add_argument("--allow-render-errors", action="store_true")
        x.add_argument(
            "--pin-random",
            type=int,
            metavar="SEED",
            help="replace Math.random and crypto.getRandomValues with a seeded generator (for works that do not seed themselves)",
        )

    b = sp.add_parser("build", help="capsule from a directory of files")
    b.add_argument("workdir")
    b.add_argument("--entry", default="index.html")
    b.add_argument("--source")
    build_args(b)
    b.set_defaults(f=cmd_build)
    c = sp.add_parser(
        "import-capture", help="capsule from a capture the device already made (FF OS offline-artworks store)"
    )
    c.add_argument("--cache-root", help="the store root (device default /home/feralfile/.cache/offline-artworks)")
    c.add_argument(
        "--source", help="the item's source URL, exactly as in the playlist (the store is keyed by its sha256)"
    )
    c.add_argument("--record", help="or: one items/<key>.json file")
    c.add_argument("--blobs", help="blobs/ directory when --record is used (default: ../blobs)")
    c.add_argument("--allow-incomplete", action="store_true")
    c.add_argument("--captured-by", default="feral-controld offlinecache")
    build_args(c)
    c.set_defaults(f=cmd_import_capture)
    v = sp.add_parser("verify")
    v.add_argument("capsule")
    v.add_argument("--png")
    v.add_argument("--engine")
    v.add_argument("--host", action="store_true")
    v.add_argument("--allow-engine-mismatch", action="store_true")
    v.add_argument("--sig", help="signature sidecar (default: <capsule>.sig.json)")
    v.add_argument("--container", action="store_true", help="already inside the engine image (see verify-in-docker.sh)")
    v.add_argument("--require-signer", metavar="DID", help="fail unless a valid signature by this did:key is present")
    v.set_defaults(f=cmd_verify)
    k = sp.add_parser("keygen")
    k.add_argument("key")
    k.set_defaults(f=cmd_keygen)
    g = sp.add_parser("sign")
    g.add_argument("capsule")
    g.add_argument("--key", required=True)
    g.add_argument("--role", default="publisher")
    g.set_defaults(f=cmd_sign)
    i = sp.add_parser("info")
    i.add_argument("capsule")
    i.set_defaults(f=cmd_info)
    a = p.parse_args()
    a.f(a)


if __name__ == "__main__":
    main()
