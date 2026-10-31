#!/usr/bin/env python3
"""dp1c render harness. Runs INSIDE the engine sandbox (or on the host with --host, a weaker claim).

Drives Chromium over the DevTools protocol pipe (fds 3/4), so the capture is defined by protocol
commands rather than by a screenshot switch:
  - device metrics are set before navigation (the page sees the pinned viewport from its first frame)
  - virtual time is paused at a fixed epoch before navigation, then granted a fixed budget; the frame is
    captured when the budget expires (Date.now() and timers are deterministic; Math.random is the work's job)
  - every failed resource load, HTTP error, and uncaught exception is recorded; the caller decides
Only stdlib. Reads one JSON config on stdin, writes shot.png and render.json into --out.
"""
import base64, http.server, json, os, socket, socketserver, struct, subprocess, sys, threading, fcntl, urllib.parse

def lo_up():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        i = struct.pack('16sH14s', b'lo', 0, b'\0' * 14)
        f = struct.unpack('16sH14s', fcntl.ioctl(s, 0x8913, i))[1]
        fcntl.ioctl(s, 0x8914, struct.pack('16sH14s', b'lo', f | 1, b'\0' * 14))
    except OSError:
        pass  # already up (host mode) or no permission (host mode)

class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a): pass

def serve(root):
    srv = socketserver.TCPServer(("127.0.0.1", 0), lambda *a, **k: Quiet(*a, directory=root, **k))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]

class CDP:
    def __init__(self, chromium, flags, cwd, prefix=()):
        # Chromium reads commands on fd 3 and writes responses on fd 4. Park the pipe ends there in the
        # parent (call this before anything else opens fds) and let them inherit; close_fds keeps the rest out.
        holders = [os.open("/dev/null", os.O_RDONLY) for _ in range(2)]   # occupy 3 and 4 so the pipes land above
        r_cmd, w_cmd = os.pipe(); r_rsp, w_rsp = os.pipe()
        for h in holders: os.close(h)
        os.dup2(r_cmd, 3); os.dup2(w_rsp, 4); os.close(r_cmd); os.close(w_rsp)
        self.p = subprocess.Popen([*prefix, chromium, *flags, "--remote-debugging-pipe", "about:blank"],
                                  pass_fds=(3, 4), cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        os.close(3); os.close(4)
        self.w = os.fdopen(w_cmd, "wb", buffering=0); self.r = os.fdopen(r_rsp, "rb", buffering=0)
        self.n = 0; self.buf = b""; self.events = []
    def _read(self):
        while b"\0" not in self.buf:
            chunk = self.r.read(65536)
            if not chunk: raise RuntimeError("chromium closed the devtools pipe")
            self.buf += chunk
        msg, _, self.buf = self.buf.partition(b"\0")
        return json.loads(msg)
    def send(self, method, params=None, session=None):
        self.n += 1; m = {"id": self.n, "method": method, "params": params or {}}
        if session: m["sessionId"] = session
        self.w.write(json.dumps(m).encode() + b"\0")
        while True:
            r = self._read()
            if r.get("id") == self.n:
                if "error" in r: raise RuntimeError(f"{method}: {r['error']}")
                return r.get("result", {})
            self.events.append(r)
    def wait_event(self, name, session, timeout_msgs=200000):
        for _ in range(timeout_msgs):
            for i, e in enumerate(self.events):
                if e.get("method") == name and e.get("sessionId") == session:
                    return self.events.pop(i)
            self.events.append(self._read())
        raise RuntimeError(f"timed out waiting for {name}")
    def close(self):
        try: self.send("Browser.close")
        except Exception: pass
        try: self.p.wait(timeout=20)
        except Exception: self.p.kill()

def main():
    cfg = json.load(sys.stdin)
    out = cfg["out"]; os.makedirs(out, exist_ok=True)
    lo_up()   # needs CAP_NET_ADMIN; chromium itself is then started with every capability dropped (chromiumPrefix)
    cdp = CDP(cfg["chromium"], cfg["flags"], cfg.get("cwd") or "/tmp", cfg.get("chromiumPrefix", []))   # before any other fd is opened
    srv, port = serve(cfg["work"])
    url = f"http://127.0.0.1:{port}/{urllib.parse.quote(cfg['entry'])}"
    vp = cfg["viewport"]
    diag = {"loadFailures": [], "httpErrors": [], "exceptions": [], "consoleErrors": []}
    try:
        tid = cdp.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        sid = cdp.send("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        S = lambda m, p=None: cdp.send(m, p, sid)
        S("Page.enable"); S("Network.enable"); S("Runtime.enable"); S("Log.enable")
        S("Emulation.setDeviceMetricsOverride", {"width": vp["width"], "height": vp["height"],
                                                 "deviceScaleFactor": vp["deviceScaleFactor"], "mobile": False,
                                                 "screenWidth": vp["width"], "screenHeight": vp["height"]})
        S("Emulation.setTimezoneOverride", {"timezoneId": cfg.get("timezone", "UTC")})
        S("Emulation.setLocaleOverride", {"locale": cfg.get("locale", "en-US")})
        S("Network.setCacheDisabled", {"cacheDisabled": True})
        if cfg.get("randomSeed") is not None:
            # Pin the two unseeded randomness sources before any page script runs. The work is untouched;
            # the engine simply hands it a deterministic Math.random / crypto.getRandomValues for this seed.
            seed = int(cfg["randomSeed"]) & 0xFFFFFFFF
            S("Page.addScriptToEvaluateOnNewDocument", {"source": f"""
(() => {{
  let s = {seed} >>> 0;                       // mulberry32
  const rnd = () => {{ s = (s + 0x6D2B79F5) >>> 0; let t = s; t = Math.imul(t ^ (t >>> 15), t | 1);
                       t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }};
  Object.defineProperty(Math, 'random', {{ value: rnd, writable: false, configurable: false }});
  const grv = (arr) => {{ for (let i = 0; i < arr.length; i++) arr[i] = Math.floor(rnd() * 256); return arr; }};
  try {{ Object.defineProperty(crypto, 'getRandomValues', {{ value: grv, writable: false, configurable: false }}); }} catch (e) {{}}
  try {{ Object.defineProperty(crypto, 'randomUUID', {{ value: () => {{ const b = grv(new Uint8Array(16)); b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
        const h = [...b].map(x => x.toString(16).padStart(2, '0')).join(''); return `${{h.slice(0,8)}}-${{h.slice(8,12)}}-${{h.slice(12,16)}}-${{h.slice(16,20)}}-${{h.slice(20)}}`; }}, writable: false, configurable: false }}); }} catch (e) {{}}
}})();"""})
        # clock: paused at a fixed epoch before the page exists
        S("Emulation.setVirtualTimePolicy", {"policy": "pause", "initialVirtualTime": cfg["virtualEpochSeconds"]})
        S("Page.navigate", {"url": url})
        # then a fixed budget; time only advances while the page is idle on the network
        S("Emulation.setVirtualTimePolicy", {"policy": "pauseIfNetworkFetchesPending",
                                             "budget": cfg["virtualTimeBudgetMs"], "maxVirtualTimeTaskStarvationCount": 100000})
        cdp.wait_event("Emulation.virtualTimeBudgetExpired", sid)
        # drain pending events into diagnostics
        for e in cdp.events:
            if e.get("sessionId") != sid: continue
            m, p = e.get("method"), e.get("params", {})
            if m == "Network.loadingFailed": diag["loadFailures"].append({"error": p.get("errorText"), "type": p.get("type"), "requestId": p.get("requestId")})
            elif m == "Network.responseReceived" and p.get("response", {}).get("status", 200) >= 400:
                diag["httpErrors"].append({"url": p["response"].get("url"), "status": p["response"].get("status")})
            elif m == "Runtime.exceptionThrown": diag["exceptions"].append(p.get("exceptionDetails", {}).get("text"))
            elif m == "Log.entryAdded" and p.get("entry", {}).get("level") == "error": diag["consoleErrors"].append(p["entry"].get("text"))
        probe = S("Runtime.evaluate", {"expression": "JSON.stringify({w:innerWidth,h:innerHeight,dpr:devicePixelRatio,now:Date.now(),hc:navigator.hardwareConcurrency,ua:navigator.userAgent})", "returnByValue": True})["result"]["value"]
        shot = S("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False, "fromSurface": True})["data"]
        png = base64.b64decode(shot)
        open(os.path.join(out, "shot.png"), "wb").write(png)
        gpu = cdp.send("SystemInfo.getInfo").get("gpu", {}).get("auxAttributes", {})
        json.dump({"page": json.loads(probe), "diagnostics": diag, "pngBytes": len(png),
                   "glRenderer": gpu.get("glRenderer"), "softwareRendering": gpu.get("softwareRendering")},
                  open(os.path.join(out, "render.json"), "w"), indent=2, sort_keys=True)
    except Exception as ex:
        err = b""
        try:
            cdp.p.kill(); err = cdp.p.stderr.read()[-3000:]
        except Exception: pass
        sys.exit(f"harness: {ex}\nchromium stderr:\n{err.decode(errors='replace')}")
    finally:
        cdp.close(); srv.shutdown(); srv.server_close()

if __name__ == "__main__":
    main()
