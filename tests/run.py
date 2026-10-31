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
CALLBACKS = {"raf": 60, "timer": 0, "iframe": 120, "caf": 1}   # per page: one per frame; none; two documents; A ran, B cancelled
EXPECT_NOW = 1600000011000  # epoch 1600000000000 + budget 10000 + round(60 * 16.667)

def run(*args, ok=True):
    r = subprocess.run(DP1C + list(args), capture_output=True, text=True)
    if ok and r.returncode != 0: sys.exit(f"FAIL {' '.join(args)}\n{r.stdout}\n{r.stderr[-3000:]}")
    return r

def sha256(p):
    import hashlib
    return hashlib.sha256(open(p, "rb").read()).hexdigest()

def reference(capsule):
    with open(capsule, "rb") as f: data = zstandard.ZstdDecompressor().stream_reader(f).read()
    t = tarfile.open(fileobj=io.BytesIO(data))
    return json.load(t.extractfile("meta/render/reference.json")), data

def check(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  ({detail})" if detail else ""))
    if not cond: sys.exit(1)

out = tempfile.mkdtemp(prefix="dp1c-tests-")
for page in ["raf", "timer", "iframe", "caf"]:
    caps = []
    for i in (1, 2):
        cap = os.path.join(out, f"{page}-{i}.dp1c")
        run("build", os.path.join(ROOT, "tests", "pages", page), "--host", "--out", cap, "--created", CREATED)
        caps.append(cap)
    check(f"{page}: two builds give identical bytes", sha256(caps[0]) == sha256(caps[1]), sha256(caps[0])[:16])
    ref, _ = reference(caps[0])
    fc = ref["frameClock"]
    check(f"{page}: 60 ticks delivered", fc["ticked"] == 60, str(fc["ticked"]))
    check(f"{page}: {CALLBACKS[page]} animation callbacks ran", fc["callbacks"] == CALLBACKS[page], str(fc["callbacks"]))
    check(f"{page}: Date.now() at capture is {EXPECT_NOW}", ref["page"]["now"] == EXPECT_NOW, str(ref["page"]["now"]))
    check(f"{page}: no render problems recorded", not any(ref["problems"].values()))

r = run("build", os.path.join(ROOT, "tests", "pages", "worker"), "--host", "--out", os.path.join(out, "worker.dp1c"), "--created", CREATED, ok=False)
check("worker: build refused", r.returncode != 0, f"exit {r.returncode}")
check("worker: refusal names the worker", "worker" in (r.stdout + r.stderr).lower())

# tamper: one byte of the entry file, same length, repacked; verify must stop at the static checks
clean = os.path.join(out, "raf-1.dp1c")
ref, data = reference(clean)
t = tarfile.open(fileobj=io.BytesIO(data)); buf = io.BytesIO(); w = tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT)
for m in t.getmembers():
    body = t.extractfile(m).read() if m.isreg() else None
    if m.name == "assets/index.html": body = body.replace(b"raf ", b"RAF ", 1)
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
check("clean: host verify reproduces the host build", v.get("exact") is True and v.get("verdict") in ("reproduced", "reproducedOnDifferentEngine"), f"verdict {v.get('verdict')}, exit {r.returncode}")
print("all checks passed;", out)
