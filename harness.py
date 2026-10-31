#!/usr/bin/env python3
"""dp1c render harness. Runs INSIDE the engine sandbox (or on the host with --host, a weaker claim).

Drives Chromium over the DevTools protocol pipe (fds 3/4), so the capture is defined by protocol
commands rather than by a screenshot switch:
  - device metrics are set before navigation (the page sees the pinned viewport from its first frame)
  - virtual time is paused at a fixed epoch before navigation, then granted a settle budget; after it the
    engine runs the frame schedule (N animation frames it ticks itself), then captures
  - every failed resource load, HTTP error, replay miss and uncaught exception is recorded; the caller decides
Two ways to feed the work to the page:
  - files:  the asset directory is served over loopback and the entry is a file in it
  - replay: the page navigates to the work's real URL and every request is answered from a table of
            captured responses (method+URL -> status, headers, body blob), exactly as FF OS's offline
            replay does on the device. A request not in the table fails (net::ERR_FAILED) and is recorded.
Only stdlib. Reads one JSON config on stdin, writes shot.png and render.json into --out.
"""

import base64, http.server, json, os, re, secrets, select, socket, socketserver, struct, subprocess, sys, threading, time, fcntl, urllib.parse

MAX_BODY = 200 * 1024 * 1024  # one DevTools message carries the body; larger captures are refused at import and here
# response headers a replay may carry: the ones that change how the browser treats a response (FF OS offlinecache's
# replayableResponseHeaders) plus the declared content type. Nothing else from a capture reaches the browser.
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
HEX64 = re.compile(r"^[0-9a-f]{64}$")
DEBUG = bool(os.environ.get("DP1C_DEBUG"))


def lo_up():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        i = struct.pack("16sH14s", b"lo", 0, b"\0" * 14)
        f = struct.unpack("16sH14s", fcntl.ioctl(s, 0x8913, i))[1]
        fcntl.ioctl(s, 0x8914, struct.pack("16sH14s", b"lo", f | 1, b"\0" * 14))
    except OSError:
        pass  # already up (host mode) or no permission (host mode)


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


def serve(root, port=0):
    srv = socketserver.TCPServer(("127.0.0.1", port), lambda *a, **k: Quiet(*a, directory=root, **k))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


class CDP:
    """One pipe, many sessions. Responses are filed by id; events go to registered waiters, else to the handler,
    else to a queue. Events are delivered while a command is outstanding (Page.navigate does not answer until an
    intercepted main-document request is fulfilled), but never re-entrantly: a handler that calls send() has the
    events arriving meanwhile queued and delivered after it returns, so the recursion depth is bounded."""

    def __init__(self, chromium, flags, cwd, prefix=()):
        # Chromium reads commands on fd 3 and writes responses on fd 4. Park the pipe ends there in the
        # parent (call this before anything else opens fds) and let them inherit; close_fds keeps the rest out.
        holders = [os.open("/dev/null", os.O_RDONLY) for _ in range(2)]  # occupy 3 and 4 so the pipes land above
        r_cmd, w_cmd = os.pipe()
        r_rsp, w_rsp = os.pipe()
        for h in holders:
            os.close(h)
        os.dup2(r_cmd, 3)
        os.dup2(w_rsp, 4)
        os.close(r_cmd)
        os.close(w_rsp)
        self.p = subprocess.Popen(
            [*prefix, chromium, *flags, "--remote-debugging-pipe", "about:blank"],
            pass_fds=(3, 4),
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        os.close(3)
        os.close(4)
        self.w = os.fdopen(w_cmd, "wb", buffering=0)
        self.r = os.fdopen(r_rsp, "rb", buffering=0)
        self.n = 0
        self.buf = b""
        self.inbox = []
        self.events = []
        self.responses = {}
        self.waiters = {}
        self.handler = None
        self.delivering = False
        self.deadline = None

    def _read(self):
        while b"\0" not in self.buf:
            if self.deadline is not None:
                left = self.deadline - time.monotonic()
                if left <= 0 or not select.select([self.r], [], [], left)[0]:
                    raise TimeoutError("wall-clock limit reached")
            chunk = self.r.read(65536)
            if not chunk:
                raise RuntimeError("chromium closed the devtools pipe")
            self.buf += chunk
        msg, _, self.buf = self.buf.partition(b"\0")
        m = json.loads(msg)
        if DEBUG and "method" in m:
            print("event", m["method"], json.dumps(m.get("params", {}))[:200], file=sys.stderr)
        return m

    def _pump(self):
        m = self._read()
        if "id" in m:
            self.responses[m["id"]] = m
        else:
            self.inbox.append(m)

    def _deliver(self):
        if self.delivering:
            return
        self.delivering = True
        try:
            while self.inbox:
                e = self.inbox.pop(0)
                key = (e.get("method"), e.get("sessionId"))
                if key in self.waiters:
                    self.waiters[key].append(e)
                elif self.handler:
                    self.handler(e)
                else:
                    self.events.append(e)
        finally:
            self.delivering = False

    def send(self, method, params=None, session=None):
        self.n += 1
        mid = self.n
        m = {"id": mid, "method": method, "params": params or {}}
        if session:
            m["sessionId"] = session
        if DEBUG:
            print("send", method, file=sys.stderr)
        self.w.write(json.dumps(m).encode() + b"\0")
        while mid not in self.responses:
            self._pump()
            self._deliver()
        r = self.responses.pop(mid)
        if "error" in r:
            raise RuntimeError(f"{method}: {r['error']}")
        return r.get("result", {})

    def expect(self, name, session):
        """Register for an event BEFORE sending the command that causes it, so it cannot be consumed elsewhere."""
        self.waiters.setdefault((name, session), [])

    def run_until(self, name, session, limit=5000000):
        key = (name, session)
        lst = self.waiters.setdefault(key, [])
        for _ in range(limit):
            if lst:
                e = lst.pop(0)
                if not lst:
                    del self.waiters[key]
                return e
            self._pump()
            self._deliver()
        raise RuntimeError(f"timed out waiting for {name}")

    def close(self):
        try:
            self.send("Browser.close")
        except Exception:
            pass
        try:
            self.p.wait(timeout=20)
        except Exception:
            self.p.kill()


def replay_table(resources):
    """method+URL -> captured response. Empty method means GET (the FF OS record convention)."""
    t = {}
    for r in resources:
        t[(r.get("method") or "GET").upper() + " " + r["url"]] = r
    return t


FRAME_CLOCK = """
(() => {
  const NONCE = %s;
  let q = [], id = 0, frozen = false; const cancelled = new Set();
  // performance.now() is the engine's clock too. Under virtual time the real one carries sub-millisecond jitter
  // (10983.3 vs 10983.4 on two runs), and works that drive a shader from it render differently each run. Before
  // the frame schedule it is whole virtual milliseconds since the document started; during the schedule it is the
  // frame's timestamp, frozen within a frame and advancing one interval per tick, the same value the
  // requestAnimationFrame callbacks receive. Writable so a work that installs its own clock is left alone.
  const origin = Date.now(); let frameT = null;
  Object.defineProperty(Performance.prototype, 'now', {value: function () { return frameT !== null ? frameT : Date.now() - origin; }, writable: true, configurable: false});
  const lock = (k, v, writable) => Object.defineProperty(window, k, {value: v, writable, configurable: false});
  // The work may replace these (a polyfill that assigns is common); doing so opts the work out of the frame clock
  // and onto timers, which virtual time covers. The control surface below cannot be reached without the nonce.
  lock('requestAnimationFrame', (cb) => { const h = ++id; if (!frozen) q.push([h, cb]); return h; }, true);
  lock('cancelAnimationFrame', (h) => { cancelled.add(h); q = q.filter(([x]) => x !== h); }, true);
  lock('__dp1c_tick', (nonce, last, t) => {
    if (nonce !== NONCE) throw new Error('dp1c: the frame clock is the engine\\'s');
    frameT = t;
    const cur = q; q = []; const errors = []; let fired = 0;
    for (const [h, cb] of cur) { if (cancelled.has(h)) continue; fired++; try { cb(t); } catch (e) { errors.push(String(e && e.stack || e)); } }
    cancelled.clear(); if (last) frozen = true;
    return JSON.stringify({fired, errors});
  }, false);
})();"""


def main():
    cfg = json.load(sys.stdin)
    global DEBUG
    DEBUG = DEBUG or bool(cfg.get("debug"))
    out = cfg["out"]
    os.makedirs(out, exist_ok=True)
    lo_up()  # needs CAP_NET_ADMIN; chromium itself is then started with every capability dropped (chromiumPrefix)
    cdp = CDP(
        cfg["chromium"], cfg["flags"], cfg.get("cwd") or "/tmp", cfg.get("chromiumPrefix", [])
    )  # before any other fd is opened
    srv, port = serve(
        cfg["work"], int(cfg.get("loopbackPort") or 0)
    )  # the pin fixes the port, so the page sees the same origin every run
    replay = cfg.get("replay")
    table = replay_table(replay["resources"]) if replay else None
    if replay:
        url = replay["entry"]
    else:
        # the entry may carry a query (a work seeded through its URL, fxhash style): the path is quoted, the query kept
        epath, _, equery = cfg["entry"].partition("?")
        url = f"http://127.0.0.1:{port}/{urllib.parse.quote(epath)}" + (f"?{equery}" if equery else "")
    vp = cfg["viewport"]
    frames, interval = int(cfg.get("frames") or 0), float(cfg.get("frameIntervalMs") or 16.667)
    nonce = secrets.token_hex(16)
    diag = {
        "loadFailures": [],
        "httpErrors": [],
        "exceptions": [],
        "consoleErrors": [],
        "replayMisses": [],
        "replayed": 0,
        "entryFulfilled": False,
        "framesTicked": 0,
        "rafCallbacks": 0,
        "frameContexts": 0,
        "otherTargets": [],
    }
    kept = []
    inflight = {}
    contexts = {}
    cdp.deadline = time.monotonic() + float(cfg.get("wallTimeoutSeconds") or 900)
    try:
        tid = cdp.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        sid = cdp.send("Target.attachToTarget", {"targetId": tid, "flatten": True})["sessionId"]
        S = lambda m, p=None: cdp.send(m, p, sid)

        def fail(rid, key, why=""):
            diag["replayMisses"].append(key + (f" ({why})" if why else ""))
            S("Fetch.failRequest", {"requestId": rid, "errorReason": "Failed"})

        def answer(p):
            """Fulfil one paused request from the capture table, the way the device's replay does."""
            req = p["request"]
            rid = p["requestId"]
            method = (req.get("method") or "GET").upper()
            u = req["url"].split("#", 1)[0]
            key = method + " " + u
            res, head = table.get(key), method == "HEAD"
            if res is None and head:
                res = table.get("GET " + u)  # a HEAD is answered from the captured GET, body omitted
            if res is None or not isinstance(res.get("status"), int) or res["status"] <= 0:
                fail(rid, key)
                return
            headers = [
                {"name": k, "value": v}
                for k, v in (res.get("headers") or {}).items()
                if isinstance(k, str) and isinstance(v, str) and k.lower() in REPLAY_HEADERS
            ]
            names = {h["name"].lower() for h in headers}
            if res.get("contentType") and "content-type" not in names:
                headers.append({"name": "Content-Type", "value": str(res["contentType"])})
            params = {"requestId": rid, "responseCode": res["status"]}
            if res.get("redirectTo"):
                if not (300 <= res["status"] <= 399) or not str(res["redirectTo"]).startswith(("http://", "https://")):
                    fail(rid, key, "bad redirect")
                    return
                headers.append({"name": "Location", "value": res["redirectTo"]})
            elif res.get("sha256") and not head:
                sha = res["sha256"]
                if not isinstance(sha, str) or not HEX64.match(sha):
                    fail(rid, key, "bad digest")
                    return
                path = os.path.join(cfg["work"], sha)
                if not os.path.isfile(path) or os.path.getsize(path) > MAX_BODY:
                    fail(rid, key, "no body")
                    return
                params["body"] = base64.b64encode(open(path, "rb").read()).decode()
            params["responseHeaders"] = headers
            S("Fetch.fulfillRequest", params)
            diag["replayed"] += 1
            if u == replay["entry"] and method == "GET":
                diag["entryFulfilled"] = True

        def on_event(e):
            m, p = e.get("method"), e.get("params", {})
            if e.get("sessionId") != sid:
                kept.append(e)
                return
            if m == "Network.requestWillBeSent":
                inflight[p["requestId"]] = p["request"]["url"]
            elif m in ("Network.loadingFinished", "Network.loadingFailed"):
                inflight.pop(p.get("requestId"), None)
            elif m == "Runtime.executionContextCreated":
                c = p["context"]
                if c.get("auxData", {}).get("isDefault"):
                    contexts[c["id"]] = c.get("auxData", {}).get("frameId")
            elif m == "Runtime.executionContextDestroyed":
                contexts.pop(p.get("executionContextId"), None)
            elif m == "Runtime.executionContextsCleared":
                contexts.clear()
            elif m == "Target.attachedToTarget":
                ti = p.get("targetInfo", {})
                diag["otherTargets"].append({"type": ti.get("type"), "url": ti.get("url")})
            if replay and m == "Fetch.requestPaused":
                answer(p)
            else:
                kept.append(e)

        cdp.handler = on_event

        S("Page.enable")
        S("Network.enable")
        S("Runtime.enable")
        S("Log.enable")
        # workers and out-of-process frames run outside this session's clock and interception; see them so the
        # render record can say so (they are reported as a render problem, not silently accepted)
        S("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True})
        S(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": vp["width"],
                "height": vp["height"],
                "deviceScaleFactor": vp["deviceScaleFactor"],
                "mobile": False,
                "screenWidth": vp["width"],
                "screenHeight": vp["height"],
            },
        )
        S("Emulation.setTimezoneOverride", {"timezoneId": cfg.get("timezone", "UTC")})
        S("Emulation.setLocaleOverride", {"locale": cfg.get("locale", "en-US")})
        try:
            S(
                "Emulation.setHardwareConcurrencyOverride",
                {"hardwareConcurrency": int(cfg.get("hardwareConcurrency") or 4)},
            )
        except RuntimeError as ex:
            diag["consoleErrors"].append(f"hardwareConcurrency not pinned: {ex}")
        S("Network.setCacheDisabled", {"cacheDisabled": True})
        if replay:
            S("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]})
        if cfg.get("randomSeed") is not None:
            # Pin the two unseeded randomness sources before any page script runs. The work is untouched;
            # the engine simply hands it a deterministic Math.random / crypto.getRandomValues for this seed.
            seed = int(cfg["randomSeed"]) & 0xFFFFFFFF
            S(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": f"""
(() => {{
  let s = {seed} >>> 0;                       // mulberry32
  const rnd = () => {{ s = (s + 0x6D2B79F5) >>> 0; let t = s; t = Math.imul(t ^ (t >>> 15), t | 1);
                       t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }};
  Object.defineProperty(Math, 'random', {{ value: rnd, writable: false, configurable: false }});
  const grv = (arr) => {{ for (let i = 0; i < arr.length; i++) arr[i] = Math.floor(rnd() * 256); return arr; }};
  try {{ Object.defineProperty(crypto, 'getRandomValues', {{ value: grv, writable: false, configurable: false }}); }} catch (e) {{}}
  try {{ Object.defineProperty(crypto, 'randomUUID', {{ value: () => {{ const b = grv(new Uint8Array(16)); b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
        const h = [...b].map(x => x.toString(16).padStart(2, '0')).join(''); return `${{h.slice(0,8)}}-${{h.slice(8,12)}}-${{h.slice(12,16)}}-${{h.slice(16,20)}}-${{h.slice(20)}}`; }}, writable: false, configurable: false }}); }} catch (e) {{}}
}})();"""
                },
            )
        if frames:
            # The frame clock is the engine's, not the compositor's: requestAnimationFrame callbacks run only when the
            # harness ticks, once per frame, with the frame's virtual time as their timestamp (computed here, not read from
            # performance.now(), which carries sub-millisecond jitter under virtual time: 10983.3 vs 10983.4). After the last
            # tick the queue freezes, so the captured frame is frame N whatever the wall clock did. Without this the
            # number of animation frames before the screenshot varies run to run (observed on a frame-counted fade).
            S("Page.addScriptToEvaluateOnNewDocument", {"source": FRAME_CLOCK % json.dumps(nonce)})
        # clock: paused at a fixed epoch before the page exists
        S("Emulation.setVirtualTimePolicy", {"policy": "pause", "initialVirtualTime": cfg["virtualEpochSeconds"]})
        S("Page.navigate", {"url": url})
        # then a settle budget; time only advances while the page is idle on the network
        cdp.expect("Emulation.virtualTimeBudgetExpired", sid)
        S(
            "Emulation.setVirtualTimePolicy",
            {
                "policy": "pauseIfNetworkFetchesPending",
                "budget": cfg["virtualTimeBudgetMs"],
                "maxVirtualTimeTaskStarvationCount": 100000,
            },
        )
        cdp.run_until("Emulation.virtualTimeBudgetExpired", sid)
        for i in range(frames):
            # one frame: run the queued animation callbacks in every document of the page (same-process frames), then
            # let virtual time move one interval, waiting on any fetch the frame started
            last = "true" if i == frames - 1 else "false"
            t = round(
                float(cfg["virtualTimeBudgetMs"]) + i * interval, 3
            )  # frame i's DOMHighResTimeStamp: settle budget plus i intervals
            for cid in list(contexts):
                try:
                    r = S(
                        "Runtime.evaluate",
                        {
                            "expression": f"__dp1c_tick({json.dumps(nonce)}, {last}, {t})",
                            "returnByValue": True,
                            "contextId": cid,
                        },
                    )
                except RuntimeError as ex:
                    if "context" in str(ex).lower():
                        contexts.pop(cid, None)
                        continue
                    raise
                if "exceptionDetails" in r:
                    diag["exceptions"].append(
                        "frame clock: "
                        + str(
                            r["exceptionDetails"].get("exception", {}).get("description")
                            or r["exceptionDetails"].get("text")
                        )
                    )
                    continue
                v = json.loads(r.get("result", {}).get("value") or "{}")
                diag["rafCallbacks"] += int(v.get("fired") or 0)
                diag["exceptions"].extend("animation frame: " + x for x in v.get("errors", []))
            diag["framesTicked"] = i + 1
            cdp.expect("Emulation.virtualTimeBudgetExpired", sid)
            S(
                "Emulation.setVirtualTimePolicy",
                {
                    "policy": "pauseIfNetworkFetchesPending",
                    "budget": interval,
                    "maxVirtualTimeTaskStarvationCount": 100000,
                },
            )
            cdp.run_until("Emulation.virtualTimeBudgetExpired", sid)
        diag["frameContexts"] = len(contexts)
        # let anything already on the pipe land (a request paused in the last frame is answered here), then capture
        quiet_until = time.monotonic() + 0.25
        while time.monotonic() < quiet_until:
            if select.select([cdp.r], [], [], 0.05)[0] or cdp.buf:
                cdp._pump()
                cdp._deliver()
                quiet_until = time.monotonic() + 0.25
        probe = S(
            "Runtime.evaluate",
            {
                "expression": "JSON.stringify({w:innerWidth,h:innerHeight,dpr:devicePixelRatio,now:Date.now(),hc:navigator.hardwareConcurrency,ua:navigator.userAgent,href:location.href,frames:window.frames.length})",
                "returnByValue": True,
            },
        )["result"]["value"]
        shot = S("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False, "fromSurface": True})[
            "data"
        ]
        gpu = cdp.send("SystemInfo.getInfo").get("gpu", {}).get("auxAttributes", {})
        cdp.handler = None
        # everything observed, including during the capture itself, goes into diagnostics
        for e in kept + cdp.events + cdp.inbox:
            if e.get("sessionId") != sid:
                continue
            m, p = e.get("method"), e.get("params", {})
            if m == "Network.loadingFailed":
                diag["loadFailures"].append(
                    {"error": p.get("errorText"), "type": p.get("type"), "url": inflight.get(p.get("requestId"))}
                )
            elif m == "Network.responseReceived" and p.get("response", {}).get("status", 200) >= 400:
                diag["httpErrors"].append({"url": p["response"].get("url"), "status": p["response"].get("status")})
            elif m == "Runtime.exceptionThrown":
                diag["exceptions"].append(
                    p.get("exceptionDetails", {}).get("exception", {}).get("description")
                    or p.get("exceptionDetails", {}).get("text")
                )
            elif m == "Log.entryAdded" and p.get("entry", {}).get("level") == "error":
                diag["consoleErrors"].append(p["entry"].get("text"))
        diag["inflightAtCapture"] = sorted(set(inflight.values()))
        png = base64.b64decode(shot)
        open(os.path.join(out, "shot.png"), "wb").write(png)
        json.dump(
            {
                "page": json.loads(probe),
                "diagnostics": diag,
                "pngBytes": len(png),
                "mode": "replay" if replay else "files",
                "glRenderer": gpu.get("glRenderer"),
                "softwareRendering": gpu.get("softwareRendering"),
            },
            open(os.path.join(out, "render.json"), "w"),
            indent=2,
            sort_keys=True,
        )
    except Exception as ex:
        err = b""
        try:
            cdp.p.kill()
            err = cdp.p.stderr.read()[-3000:]
        except Exception:
            pass
        sys.exit(
            f"harness: {ex}\nstill in flight: {json.dumps(inflight)}\ndiagnostics: {json.dumps(diag)}\nchromium stderr:\n{err.decode(errors='replace')}"
        )
    finally:
        cdp.close()
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
