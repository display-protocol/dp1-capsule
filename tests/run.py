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
    v.get("exact") is True and v.get("verdict") == "reproducedUnpinned" and r.returncode == 2,
    f"verdict {v.get('verdict')}, exit {r.returncode}",
)

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
