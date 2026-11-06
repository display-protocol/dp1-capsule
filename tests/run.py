#!/usr/bin/env python3
"""Re-measure the frame-clock and tamper claims in README.md, in host mode (this machine's Chromium under bubblewrap).

  python3 tests/run.py            # ~2 minutes; prints one line per check, exits non-zero on the first failure

Each page is built twice with a fixed --created; the two capsules must be byte-identical and the page must have
seen exactly 60 frames and Date.now() == epoch + budget + 60 * 16.667 ms. The worker page must be refused at build,
naming the worker. A capsule with one byte of its entry file changed must verify as assetsCorrupt before rendering."""

import io, json, os, subprocess, sys, tarfile, tempfile
import zstandard

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DP1C = [sys.executable, os.path.join(ROOT, "dp1c.py")]
CREATED = "2026-10-30T00:00:00Z"
CALLBACKS = {
    "raf": 60,
    "timer": 0,
    "iframe": 120,
    "caf": 1,
    "perf": 60,
}  # per page: one per frame; none; two documents; A ran, B cancelled; one that reads performance.now() itself
EXPECT_NOW = 1600000011000  # epoch 1600000000000 + budget 10000 + round(60 * 16.667)


def run(*args, ok=True):
    r = subprocess.run(DP1C + list(args), capture_output=True, text=True)
    if ok and r.returncode != 0:
        sys.exit(f"FAIL {' '.join(args)}\n{r.stdout}\n{r.stderr[-3000:]}")
    return r


def sha256(p):
    import hashlib

    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def reference(capsule):
    with open(capsule, "rb") as f:
        data = zstandard.ZstdDecompressor().stream_reader(f).read()
    t = tarfile.open(fileobj=io.BytesIO(data))
    return json.load(t.extractfile("meta/render/reference.json")), data


def dp1c_tolerance_default():
    sys.path.insert(0, ROOT)
    import dp1c

    return dp1c.DEFAULT_TOLERANCE


def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    if not cond:
        sys.exit(1)


out = tempfile.mkdtemp(prefix="dp1c-tests-")
for page in ["raf", "timer", "iframe", "caf", "perf"]:
    caps = []
    for i in (1, 2):
        cap = os.path.join(out, f"{page}-{i}.dp1c")
        run("build", os.path.join(ROOT, "tests", "pages", page), "--host", "--out", cap, "--created", CREATED)
        caps.append(cap)
    check(f"{page}: two builds give identical bytes", sha256(caps[0]) == sha256(caps[1]), sha256(caps[0])[:16])
    ref, _ = reference(caps[0])
    fc = ref["frameClock"]
    check(f"{page}: 60 ticks delivered", fc["ticked"] == 60, str(fc["ticked"]))
    check(
        f"{page}: {CALLBACKS[page]} animation callbacks ran", fc["callbacks"] == CALLBACKS[page], str(fc["callbacks"])
    )
    check(f"{page}: Date.now() at capture is {EXPECT_NOW}", ref["page"]["now"] == EXPECT_NOW, str(ref["page"]["now"]))
    check(f"{page}: no render problems recorded", not any(ref["problems"].values()))

# an entry with a query: the work sees it in location.href, the capsule records it, two builds agree, verify is exact
caps = []
for i in (1, 2):
    cap = os.path.join(out, f"raf-query-{i}.dp1c")
    run(
        "build",
        os.path.join(ROOT, "tests", "pages", "raf"),
        "--host",
        "--out",
        cap,
        "--created",
        CREATED,
        "--entry",
        "index.html?fxhash=0xabc&fxiteration=1",
    )
    caps.append(cap)
check("query entry: two builds give identical bytes", sha256(caps[0]) == sha256(caps[1]))
ref, _ = reference(caps[0])
check(
    "query entry: recorded and seen by the page",
    ref["entry"] == "index.html?fxhash=0xabc&fxiteration=1"
    and ref["page"]["href"].endswith("/index.html?fxhash=0xabc&fxiteration=1"),
    ref["page"]["href"],
)
r = run("verify", caps[0], "--host", ok=False)
check("query entry: verifies exact", json.loads(r.stdout).get("exact") is True, json.loads(r.stdout).get("verdict"))

r = run(
    "build",
    os.path.join(ROOT, "tests", "pages", "worker"),
    "--host",
    "--out",
    os.path.join(out, "worker.dp1c"),
    "--created",
    CREATED,
    ok=False,
)
check("worker: build refused", r.returncode != 0, f"exit {r.returncode}")
check("worker: refusal names the worker", "worker" in (r.stdout + r.stderr).lower())

# tamper: one byte of the entry file, same length, repacked; verify must stop at the static checks
clean = os.path.join(out, "raf-1.dp1c")
ref, data = reference(clean)
t = tarfile.open(fileobj=io.BytesIO(data))
buf = io.BytesIO()
w = tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT)
for m in t.getmembers():
    body = t.extractfile(m).read() if m.isreg() else None
    if m.name == "assets/index.html":
        body = body.replace(b"raf ", b"RAF ", 1)
    w.addfile(m, io.BytesIO(body) if body is not None else None)
w.close()
tampered = os.path.join(out, "raf-tampered.dp1c")
open(tampered, "wb").write(zstandard.ZstdCompressor(level=19).compress(buf.getvalue()))
r = run("verify", tampered, "--host", ok=False)
verdict = json.loads(r.stdout).get("verdict") if r.stdout.strip().startswith("{") else None
check("tamper: verify says assetsCorrupt", verdict == "assetsCorrupt", f"verdict {verdict}, exit {r.returncode}")
check("tamper: exit 2", r.returncode == 2)

r = run("verify", clean, "--host", ok=False)
v = json.loads(r.stdout)
check(
    "clean: host verify of a host build is reproducedUnpinned, exit 2",
    v.get("exact") is True and v.get("withinTolerance") is True and v.get("tolerance") == dp1c_tolerance_default()
    and v.get("verdict") == "reproducedUnpinned" and r.returncode == 2,
    f"verdict {v.get('verdict')}, exit {r.returncode}",
)

# the verdict against the declared tolerance, offline, with the distances measured on the examples (dp1c.DEFAULT_TOLERANCE)
sys.path.insert(0, ROOT)
import dp1c

D = dp1c.decide
check("tolerance: the documented default is 3", dp1c.DEFAULT_TOLERANCE == 3, str(dp1c.DEFAULT_TOLERANCE))
T = dp1c.DEFAULT_TOLERANCE
same = dict(bad=[], signer_ok=True, sigs=[], clean=True, engine_match=True, harness_match=True, pinned="sha", same_version=True)
check("tolerance: exact render is reproduced", D(**same, exact=True, distance=0, tolerance=T) == "reproduced")
check("tolerance: a 2-bit shift is reproduced at the default", D(**same, exact=False, distance=2, tolerance=T) == "reproduced")
check("tolerance: a 4-bit crop is reproMismatch at the default", D(**same, exact=False, distance=4, tolerance=T) == "reproMismatch")
check("tolerance: an 11-bit blank is reproMismatch at the default", D(**same, exact=False, distance=11, tolerance=T) == "reproMismatch")
check("tolerance: 0 is decided by the pixel hash: distance 0, not exact, is a mismatch", D(**same, exact=False, distance=0, tolerance=0) == "reproMismatch")
check("tolerance: 0 with exact pixels is reproduced", D(**same, exact=True, distance=0, tolerance=0) == "reproduced")
check("tolerance: within, on another engine, is reproducedOnDifferentEngine", D(**{**same, "engine_match": False}, exact=False, distance=3, tolerance=T) == "reproducedOnDifferentEngine")
check("tolerance: render problems outrank a matching frame", D(**{**same, "clean": False}, exact=True, distance=0, tolerance=T) == "renderProblems")
check("tolerance: an unsigned capsule with a required signer is unsigned", D(**{**same, "signer_ok": False}, exact=True, distance=0, tolerance=T) == "unsigned")
check("tolerance: a bad static check is assetsCorrupt", D(**{**same, "bad": ["x"]}, exact=True, distance=0, tolerance=T) == "assetsCorrupt")
from PIL import Image

W, H = 1920, 1080
top = Image.new("RGB", (W, H), (255, 255, 255))
top.paste((0, 0, 0), (0, 0, W, H // 2))
bottom = Image.new("RGB", (W, H), (255, 255, 255))
bottom.paste((0, 0, 0), (0, H // 2, W, H))
check("two axes: black-top vs black-bottom is 0 bits on rows (the row hash alone would pass it)", dp1c.hamming(dp1c.dhash(top), dp1c.dhash(bottom)) == 0)
check("two axes: and far over the default on columns", dp1c.hamming(dp1c.dhash_v(top), dp1c.dhash_v(bottom)) > T, str(dp1c.hamming(dp1c.dhash_v(top), dp1c.dhash_v(bottom))))
r = run("build", os.path.join(ROOT, "tests", "pages", "raf"), "--host", "--out", os.path.join(out, "t65.dp1c"), "--created", CREATED, "--tolerance", "65", ok=False)
check("tolerance: build refuses 65", r.returncode != 0 and "0..64" in (r.stdout + r.stderr))

# an older capsule with no repro.tolerance verifies at the default
ref, data = reference(clean)
t = tarfile.open(fileobj=io.BytesIO(data))
buf = io.BytesIO()
w = tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT)
for m in t.getmembers():
    body = t.extractfile(m).read() if m.isreg() else None
    if m.name == "playlist.json":
        pl = json.loads(body)
        del pl["items"][0]["repro"]["tolerance"]
        body = json.dumps(pl, sort_keys=True, separators=(",", ":")).encode()
        m.size = len(body)
    w.addfile(m, io.BytesIO(body) if body is not None else None)
w.close()
legacy = os.path.join(out, "raf-legacy.dp1c")
open(legacy, "wb").write(zstandard.ZstdCompressor(level=19).compress(buf.getvalue()))
r = run("verify", legacy, "--host", ok=False)
v = json.loads(r.stdout)
check("tolerance: a capsule without the field verifies at the default", v.get("tolerance") == T and v.get("verdict") == "reproducedUnpinned", f"{v.get('tolerance')} {v.get('verdict')}")

# recorded build problems keep a capsule at renderProblems even when the render is clean and exact
t = tarfile.open(fileobj=io.BytesIO(data))
buf = io.BytesIO()
w = tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT)
for m in t.getmembers():
    body = t.extractfile(m).read() if m.isreg() else None
    if m.name == "meta/render/reference.json":
        rj = json.loads(body)
        rj["problems"]["exceptions"] = ["recorded at build"]
        body = json.dumps(rj, sort_keys=True, separators=(",", ":")).encode()
        m.size = len(body)
    w.addfile(m, io.BytesIO(body) if body is not None else None)
w.close()
recorded = os.path.join(out, "raf-recorded.dp1c")
open(recorded, "wb").write(zstandard.ZstdCompressor(level=19).compress(buf.getvalue()))
r = run("verify", recorded, "--host", ok=False)
v = json.loads(r.stdout)
check("recorded problems: a capsule built with problems is renderProblems on a clean render", v.get("verdict") == "renderProblems" and r.returncode == 2, f"{v.get('verdict')}")

# web3:// URL parsing (ERC-4804 auto mode), offline
import importlib.util

_spec = importlib.util.spec_from_file_location("web3_read", os.path.join(ROOT, "tools", "web3-read.py"))
web3_read = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(web3_read)
u = web3_read.parse_web3_url("web3://0x4F32740CF412D647A622BA27C36FA4B76F71601F:1/artpiece")
check(
    "web3: contract, chain and method parse",
    u["contract"] == "0x4f32740cf412d647a622ba27c36fa4b76f71601f"
    and u["chain_id"] == 1
    and u["method"] == "artpiece"
    and u["args"] == []
    and u["returns"] is None,
)
u = web3_read.parse_web3_url(
    "web3://0x953d288708bb771f969fcfd9ba0819ef506ac718/getTokenHtml/0xa7d8d9ef8D8Ce8992Df33D8b8CF4Aebabd5bD270/78000503?returns=(string)"
)
check(
    "web3: auto-mode argument types (address, uint256) and returns",
    [t for t, _ in u["args"]] == ["address", "uint256"] and u["returns"] == ["string"],
)
sel = web3_read.keccak256(b"artpiece()")[:4].hex()
check("web3: selector matches the recorded calldata for Han's contracts", sel == "37a908fc", sel)
enc = web3_read.abi_encode([("address", "0xa7d8d9ef8D8Ce8992Df33D8b8CF4Aebabd5bD270"), ("uint256", "78000503")])
check(
    "web3: ABI encoding of (address, uint256)",
    enc.hex() == "000000000000000000000000a7d8d9ef8d8ce8992df33d8b8cf4aebabd5bd270" + hex(78000503)[2:].rjust(64, "0"),
)

# signatures: the example sidecars verify with the library and without it (the engine image has none)
SIGCHECK = """
import sys, json
if sys.argv[1] == "nolib": sys.modules["cryptography"] = None
sys.path.insert(0, sys.argv[2]); import dp1c
print(json.dumps(dp1c.check_signatures(sys.argv[3], sys.argv[4])))
"""
import shutil

side = os.path.join(ROOT, "examples", "fidenza-503.dp1c.sig.json")
cap = os.path.join(ROOT, "examples", "fidenza-503.dp1c")
for mode in ("lib", "nolib"):
    r = subprocess.run([sys.executable, "-c", SIGCHECK, mode, ROOT, cap, side], capture_output=True, text=True)
    sigs = json.loads(r.stdout or "[]")
    want = "cryptography" if mode == "lib" else "pure-python"
    check(
        f"signature: example sidecar valid ({mode})",
        bool(sigs) and all(s["valid"] and s["checkedWith"] == want for s in sigs),
        r.stdout.strip()[:120] or r.stderr[-300:],
    )
bad = json.load(open(side))
sig = bad["signatures"][0]["sig"]
bad["signatures"][0]["sig"] = ("A" if sig[0] != "A" else "B") + sig[1:]
badside = os.path.join(out, "bad.sig.json")
json.dump(bad, open(badside, "w"))
r = subprocess.run([sys.executable, "-c", SIGCHECK, "nolib", ROOT, cap, badside], capture_output=True, text=True)
sigs = json.loads(r.stdout or "[]")
check(
    "signature: one flipped character is invalid (pure-python)",
    bool(sigs) and not any(s["valid"] for s in sigs),
    r.stdout.strip()[:120],
)
print("all checks passed;", out)
