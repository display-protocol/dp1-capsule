# DP-1 capsule: proposed text for §5, §9 and the engine layer

Draft for discussion against DP-1 v1.1.0, written from a working prototype (`dp1c.py`, `harness.py`, `engine/build-engine.sh`). Reviewed read-only by Codex (gpt-6-astra) and Grok on 2026-10-30; the revisions below answer their findings. Open decisions for the team are at the end.

## The claim a capsule makes

A capsule is a file that lets anyone, offline, with no party to the original sale present, run a work and see that it is still the work. Two promises are kept separately and both are checked:

1. **The work is intact and reachable.** Every file the work loads is inside, content-hashed; the manifest must cover the tree exactly (no unlisted files, no missing files); the playlist's `repro.assetsSHA256` must agree with the manifest. The files were resolved at build time by public schemes only (next section) and frozen, so no host has to exist for the work to open.
2. **The work runs.** The pinned engine, run with the network namespace unshared, the clock pinned, and the viewport set before navigation, renders it with no render problems, to a frame within the tolerance the artist declared (`repro.tolerance`, a dHash distance; 0 is exact pixels) of the frame the publisher witnessed.

Promise 1 is the one that has failed in the field: when fxhash closed on 2026-08-17, There's No Distance #1 was intact on chain and unplayable everywhere, because every path to it ran through one host. Promise 2 is witnessed, not argued. Whether two renders that differ by a few pixels or a shade are the same work is a question the field has argued without resolution (Casey Reas, 2026-11-06); what the artist fixed is the script and its seed, and the engine's output at a given time is witnessed by whoever signs the capsule. Exact pixels is a tolerance of 0, the artist's to ask for, and the engine pin exists so the work runs at all in 2036, not to make pixels equal.

## What v1.1.0 says today (quoted, so the delta is reviewable)

§5: "`repro`: engineVersion, seed, assetsSHA256, frameHash {sha256, phash}. Players MUST compute the first-frame SHA-256; discrepancy triggers `reproMismatch`. If an artwork requires a derived seed, the logic MUST be contained within the artwork payload; DP-1 does not standardize seedScript execution at this time."

§9: "`<playlist>.json` / `/assets/<…>` / `/meta/engine/<layer>`. Tar + Zstd; SHA-256 of archive = capsule CID."

## Where a work comes from: public schemes, never one-offs

A player resolves a playlist `source` by its URI scheme and by nothing else. The schemes are public standards, each implemented once: `ipfs://` and `ar://` (content-addressed, the bytes verified against the address), `onchfs://` (fxhash's on-chain file system; content-addressed, verified the same way; the one gateway that served it died with fxhash on 2026-08-17), `web3://` (ERC-4804: the work is the return of a contract call, trusted through a node, which covers Han's `artpiece()` and Art Blocks' `getTokenHtml` alike), `tezos-storage:` (TZIP-16, the Tezos form of the same idea) and `data:` for works small enough to travel inside the signed playlist. Anything particular to one artist belongs in that artist's contract, never in the player; a player that needs to know a function name has already failed. The capsule is the same rule applied once more: it resolves at build time, by these schemes, and freezes what it got.

## §9 Capsule format (replaces the quoted text)

```
playlist.json                 DP-1 playlist, exactly one item (see decision 3), with repro and provenance blocks
assets/                       the work: every file it loads, nothing else (two layouts, below)
meta/engine/chromium.json     the engine layer (below)
meta/capture/resources.json   replay layout only: the captured responses (method, URL, status, headers, body sha256)
meta/render/reference.json    mode, entry, asset manifest {relpath: sha256}, frame record, page probe, render problems
meta/render/reference.png     the reference frame
```

**Archive.** USTAR tar with regular-file entries only, no directory entries, paths sorted bytewise, mtime 0, uid/gid 0, empty uname/gname, mode 0644; then zstd level 19, single frame, content size written, no checksum frame. The capsule id is the sha256 of the archive bytes. All JSON files are canonical: keys sorted, separators `,` and `:`, UTF-8, no trailing newline. Playlist and item ids are UUID v5 over the URL namespace of `dp1c:playlist:<fp>` and `dp1c:item:<fp>`, where `<fp>` is `<source>|<relpath>=<sha256>|…` over the sorted manifest. Same inputs, same bytes, same id. (Measured: two builds of the Fidenza example produce identical files.)

**Two layouts, one verifier.** `reference.json.mode` is `files` or `replay`.

- `files`: `assets/` is a directory tree and `entry` is a plain file name at its root, optionally followed by a query string the work reads (fxhash-style seeding, `index.html?fxhash=0x…`); the path is percent-encoded into the URL, the query is kept verbatim, and both are recorded, so the seed a work takes from its URL is part of the payload (§5). Players serve `assets/` over loopback HTTP from inside the sandbox and navigate to `http://127.0.0.1:<port>/<entry>`. For works whose files are already in hand (on-chain generators, artist uploads).
- `replay`: `assets/<sha256>` holds each response body the work loaded, content-addressed, and `meta/capture/resources.json` is the table the device recorded while running the work once: for every request, method, URL, status, the origin's declared `Content-Type`, the CORS and cross-origin headers that affect how the browser treats the response, a `Location` for redirects, and the body's sha256. `entry` is the work's real URL (`item.source`). The player navigates to that URL with request interception on and answers every request from the table, exactly as FF OS's offline replay does on the device (`feral-controld` `offlinecache`, keyed `METHOD URL`, a HEAD answered from the captured GET, a miss failed with a network error). A miss is recorded as a render problem and the build refuses. This is the layout the device produces for every work it plays; the files layout is the special case where no capture was needed.

  Static checks a verifier runs before rendering anything (failing any is `assetsCorrupt`): the entry is an http(s) URL with a GET record; every record is GET or HEAD with a status in 100..599 and a unique method+URL; a redirect record has a 3xx status, an http(s) target and no body; every body digest is 64 lowercase hex and names the asset at exactly `assets/<digest>` whose manifest hash is that digest; every asset is referenced by a record; response headers are limited to the set that changes how the browser treats a response (the CORS and cross-origin headers FF OS already allowlists, plus `Content-Type`); nothing else from a capture reaches the browser. Lookup is exact on method and URL after dropping the fragment, as on the device: a different spelling of the same URL is a miss. After rendering, the entry document must have been served from the table, the page must have landed on a captured URL, nothing may still be in flight, and no worker or out-of-process frame may have appeared (they run outside this clock and this interception), or the verdict is `renderProblems`. Prototype limits, refused at import: methods other than GET and HEAD, bodies over 200 MB, more than one record per method+URL (a sequence of changing responses to one URL is not representable).

## The engine layer

`meta/engine/chromium.json` (values from the example capsule):

```json
{
  "engine": "chromium", "version": "155.0.8059.84", "platform": "linux-x86_64", "renderer": "software",
  "flags": ["--headless=new", "--disable-gpu", "--use-angle=swiftshader", "--use-gl=angle",
            "--force-color-profile=srgb", "--disable-skia-runtime-opts", "--hide-scrollbars",
            "--font-render-hinting=none", "--disable-lcd-text", "--run-all-compositor-stages-before-draw",
            "--deterministic-mode", "--enable-begin-frame-control", "--disable-extensions",
            "--disable-background-networking", "--disable-crashpad", "--disable-crash-reporter",
            "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check", "--no-sandbox",
            "--user-data-dir=/tmp/profile"],
  "env": {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "TMPDIR": "/tmp", "XDG_CONFIG_HOME": "/tmp/xdg",
          "XDG_CACHE_HOME": "/tmp/xdgc", "XDG_RUNTIME_DIR": "/tmp/run", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TZ": "UTC"},
  "viewport": {"width": 1920, "height": 1080, "deviceScaleFactor": 1},
  "virtualEpochSeconds": 1600000000, "virtualTimeBudgetMs": 10000, "timezone": "UTC", "locale": "en-US",
  "frames": 60, "frameIntervalMs": 16.667, "hardwareConcurrency": 4,
  "loopbackPort": 8371,
  "randomSeed": null,
  "network": "none",
  "harnessSha256": "<sha256 of harness.py>",
  "imageRef": {
    "sha256": "<sha256 of engine-<sha>.tar.zst>",
    "format": "tar+zstd rootfs",
    "source": "https://archive.archlinux.org/repos/2026/10/20/",
    "chromiumPackage": "https://archive.archlinux.org/packages/c/chromium/chromium-155.0.8059.84-1-x86_64.pkg.tar.zst",
    "packages": {"chromium": "155.0.8059.84-1", "...": "..."},
    "packageFiles": {"chromium-155.0.8059.84-1-x86_64.pkg.tar.zst": "<sha256>", "...": "..."},
    "buildTools": "pacman 7.x; tar 1.35; zstd v1.5.7; python 3.14.x",
    "run": "<the exact sandbox invocation>"
  }
}
```

**`imageRef.sha256` is the pin and the only thing a verifier trusts.** It names a content-addressed root filesystem holding exactly this engine and its fonts. A verifier computes the digest of the image it is given (or reads a completed cache entry whose name and stamp equal the digest) and refuses to call a result `reproduced` under any other image. `packages`, `packageFiles`, `source` and `buildTools` are the rebuild recipe for the case where the blob is lost; a rebuild is accepted only if its digest matches. (Known limit: pacman install scriptlets run during the build; machine id and install dates are normalized afterwards; bit-exact rebuild has not yet been demonstrated.)

**The procedure is pinned too.** `harnessSha256` is the digest of the harness that drove the browser. The harness runs inside the image; its DevTools commands define the capture: device metrics before navigation, timezone and locale overrides, cache disabled, virtual time paused at `virtualEpochSeconds` before navigation, then a settle budget of `virtualTimeBudgetMs` that advances only while the page has no network fetches pending, then the frame schedule, then `Page.captureScreenshot`. `Math.random` and `crypto.getRandomValues` are left to the work by default (§5: seed logic lives in the payload). When the engine layer carries `randomSeed`, the harness installs a seeded generator (mulberry32 over the 32-bit seed) in place of both before any page script runs, and the frame then certifies one witnessed run. The work is never modified.

**The frame clock is the engine's.** Headless Chromium drives `requestAnimationFrame` from its compositor on wall-clock time, so the number of animation frames that run before a capture varies from run to run even under virtual time; measured on a work whose title fade counts frames, two renders in the same image differed. The engine therefore hands the page its own frame clock: before any script runs, `requestAnimationFrame` is replaced by a queue that fires only when the harness ticks it. After the settle budget the harness runs exactly `frames` ticks in every document of the page (the top document and same-process iframes, found through their execution contexts); each tick runs the queued callbacks with the frame's virtual time as the timestamp (the settle budget plus one interval per frame, computed by the harness; `performance.now()` under virtual time is not stable below the millisecond), honours cancellations made during the frame, reports callback exceptions as render problems, defines `performance.now()` as the same clock (whole virtual milliseconds before the schedule, the frame's timestamp during it; a work that reads the clock itself, as shader pieces do for their time uniform, then renders the same frame every run), then advances virtual time by `frameIntervalMs`, waiting on any fetch the frame started. After the last tick the queue freezes. The tick entry point is guarded by a per-run nonce the page never sees, so a work cannot tick, forge or unfreeze it; a work may replace `requestAnimationFrame` itself (polyfills assign to it), which only moves it onto timers. Timers, `Date.now()` and `performance.now()` follow virtual time; the page's `Date.now()` at capture is `virtualEpochSeconds * 1000 + virtualTimeBudgetMs + frames * frameIntervalMs`, rounded to the millisecond, and timers get that last interval after the final animation frame. `navigator.hardwareConcurrency` is pinned to 4.

  What this covers and what it does not, stated so the frame record is read correctly: a work that never calls `requestAnimationFrame` ticks 60 empty frames (`rafCallbacks` is 0 in the render record; that is a valid static capture, not evidence of animation); `setTimeout`/`setInterval` animation is deterministic under virtual time but is captured at settle plus one second of virtual time, not at "frame 60" of anything; callbacks of one frame run in one script turn, so microtask checkpoints between them differ from a live browser; same-process iframes are ticked; a worker or out-of-process iframe is detected and makes the render a `renderProblems` result rather than a silent pass; CSS animations and transitions, and video, run on the compositor's clock and are not under this one. Decision 4 lists them.

**Isolation.** The image runs under a user-namespace sandbox (bubblewrap): every namespace unshared, image read-only at `/`, `assets/` read-only at `/work`, one fresh writable directory at `/out`, the harness read-only at `/harness`, environment cleared and set to `env`, cwd `/`. Loopback is brought up inside the empty network namespace by the harness (needs CAP_NET_ADMIN), then Chromium is started through `setpriv` with the bounding set empty and no-new-privs set. Nothing outside the namespace is reachable over the network; a resource not in `assets/` fails with the kernel's own `ERR_INTERNET_DISCONNECTED`, and the build refuses to write the capsule.

**Software raster for verification, GPU for display.** The engine renders with SwiftShader (named explicitly, not by fallback), sRGB forced, Skia runtime optimizations off. A player showing the work may use the GPU. The reference frame is a witness, not a display requirement. CPU-dependent paths inside SwiftShader remain a cross-machine risk; measured so far on one pair, an x86-64 laptop under bubblewrap and an Apple Silicon Mac running the image under Rosetta in Docker, with identical hashes for both examples.

## §5 Reproduction within a declared tolerance (replaces "Players MUST compute the first-frame SHA-256")

- `frameHash.sha256` is sha256 over the ASCII prefix `"<width>x<height>:RGB8:"` followed by the decoded 8-bit RGB pixels in row-major order, of the frame captured after the settle budget and `frames` animation frames of the engine layer. It is not the first frame.
- `frameHash.phash` is a 64-bit difference hash: convert to 8-bit grayscale, resize to 9×8 with Lanczos, for each of the 8 rows emit 8 bits left to right where bit = left pixel > right pixel, pack row-major big-endian, hex with `0x`. Players report Hamming distance.
- `tolerance` is the dHash distance within which a render is the work, an integer 0..64, declared at build by the artist or publisher. 0 means exact pixels and is decided by `frameHash.sha256`, never by the distance (two frames can share a dHash and differ in every pixel). Absent, a verifier uses 3. The distance is taken on two axes: the recorded `phash` (rows, left against right) and a column hash of the same construction (8×9, upper against lower) that the verifier computes from the reference PNG in the capsule and from its render; the larger of the two distances is compared with the tolerance. The column hash is carried in `reference.json`'s frame record for new builds, not in the playlist, and is recomputed from the PNG either way, so `phash` keeps its definition. The readings behind the default are `tools/readings.py`, run on the five examples' reference frames, each transformation spelled out in the script; row/column bits, 2026-11-06:

  | transformation | E-volved | Fidenza | Digital Soul | Prodigy | No Distance |
  |---|---|---|---|---|---|
  | JPEG q60 | 1/0 | 0/0 | 0/3 | 2/2 | 1/0 |
  | blur 1.5 px | 0/0 | 0/0 | 0/3 | 0/1 | 0/2 |
  | shift 2 px | 0/0 | 0/2 | 1/3 | 2/2 | 1/1 |
  | gamma 0.8 | 1/0 | 0/0 | 0/0 | 1/2 | 1/4 |
  | brightness ×1.1 | 3/0 | 2/2 | 4/6 | 2/3 | 2/3 |
  | saturation ×1.4 | 0/0 | 0/0 | 2/6 | 2/2 | 0/0 |
  | crop 5% each edge | 3/0 | 4/12 | 3/4 | 6/8 | 17/11 |
  | blank | 11/13 | 34/18 | 12/14 | 30/29 | 26/30 |
  | halves swapped top/bottom | 20/21 | 5/28 | 20/17 | 21/29 | 34/38 |
  | mirrored left-right | 7/6 | 4/20 | 5/4 | 38/34 | 30/24 |
  | mirrored top-bottom | 10/3 | 4/15 | 4/7 | 36/32 | 32/30 |
  | rotated 180 | 11/3 | 4/15 | 7/7 | 4/4 | 28/30 |

  Another work is 13 or more on at least one axis for every pair. What the table says: engine-sized changes (JPEG, blur, a 2 px shift) cost at most 3 on any of the five; a flipped, rotated or halves-swapped frame costs at least 4 on one axis, with the symmetric works closest (Prodigy rotated 180 is 4/4); so 3 is the largest value under every flipped reading, with one bit of margin where the work is symmetric or sparse, and the column axis is what keeps Fidenza's mirror (4/15) and halves swap (5/28) out. Brightness, saturation and gamma changes of the sizes tried reach 4 to 6 on the sparse works and are over the default there; a 5% crop sits at the default on E-volved and over it elsewhere. What the measure does not see, stated so the artist's declaration is read correctly: it is 8×8 cells of grayscale order, so content smaller than a cell can vanish or move without the hash noticing; a change that keeps the luminance order between neighbors is invisible (desaturating the Fidenza reference scores 0) while one that reverses it is not (swapping red and green on stripes scores 64); and sparse or symmetric works sit closer to their own blank and their own flip. A drawing that vanished because a library failed or a script threw is caught earlier, as `renderProblems`; one that vanished silently inside a cell is what the artist's contact sheet and a per-work second measure are for.
- `assetsSHA256` lists the manifest's hashes sorted by relative path.
- A verifying player MUST run inside the engine named by `imageRef`. Verdicts, in the order they are decided: `assetsCorrupt` (promise 1 or any static check failed, a malformed `tolerance` included; nothing is rendered); `unsigned` / `signatureInvalid` (a required signer is absent or wrong); `renderProblems` (the render loaded something that failed, threw, missed the capture table, did not serve the entry from it, left a request in flight, or ran a worker or out-of-process frame, or the build recorded any of these in `reference.json`; pixels are compared and reported but do not decide; a capsule built with `--allow-render-errors` carries its problems and is reported this way every time, an acknowledged partial); `reproMismatch` (the larger of the two distances over the tolerance, or, at tolerance 0, any pixel different); `reproduced` (within tolerance, same image, same harness digest); `reproducedUnpinned` (within tolerance, same Chromium version and harness, but the capsule names no image: it was built with `--host`, so there is nothing to be inside of); `reproducedOnDifferentEngine`. The report carries `exact`, `phashDistance` (rows), `phashDistanceV` (columns), `tolerance` and `withinTolerance` whatever the verdict (null for the ones a static failure never computed, `tolerance` excepted). Only `reproduced` exits 0.

## Decisions for the team

1. **Signing: decided, whole capsule.** A detached sidecar `<capsule>.dp1c.sig.json` carries `capsuleId` and a `signatures` array in the §7 object shape (`alg`, `kid` as `did:key`, `ts`, `payload_hash`, `role`, `sig`), where `payload_hash` is `sha256:<capsule id>` and `sig` is Ed25519 over that string, exactly as §7 signs a playlist's payload hash. The signature covers every byte of the capsule, so the frame record and the assets cannot be swapped independently. A verifier given `--require-signer <did:key>` refuses anything not validly signed by that key. The playlist inside stays unsigned; §9 should say a capsule's playlist is signed by the capsule signature and is exempt from §7.1.3's rejection rule when played from a verified capsule. Remaining: whose key signs in production (Feral File as publisher, the artist as a second signature), and where the kid is published. The artist's signature is the sign-off: with `role` `artist`, it says this frame, in this engine, at this time, within this tolerance, is the work, and it is the standard for that work, one artist's declaration rather than a rule for all (Sean, 2026-11-06). Proposed so no artist needs a new key: a second signature form, `alg` `eip191`, `kid` as `did:pkh:eip155:<chain>:<address>`, `sig` the wallet's `personal_sign` over the same `sha256:<capsule id>` string, verified by recovering the address. The capsule id is then the thing to publish, anywhere: the feed, a contract, a post. Not built.
2. **UUID version.** Content-derived UUID v5 ids give reproducible capsules; the Go validator and the schema text expect v4. Recommendation: schema says "UUID", validator accepts v4 and v5.
3. **Items.** One item per capsule, or a per-item reference record. Recommendation: one, until a real multi-item case exists.
4. **Frame schedule: mechanism decided, choice open.** Randomness in the main document is handled (the work seeds itself, or `randomSeed` pins `Math.random` and `crypto.getRandomValues`) and so is the main-document frame clock (`frames`, `frameIntervalMs`). What remains is which frame: 60 is a default, not an artist's statement. Proposal: `repro.frames` as a list of (frame, frameHash) the artist or publisher declares, default one entry at 60. Not under the engine's clock yet: out-of-process iframes and workers (attach them as targets and install the same clock), CSS animations, transitions and video (compositor time), and interactive works (declared inputs). A work in one of those classes can still reproduce by luck or never; the render record says what ran (`rafCallbacks`, `frameContexts`) so a reader can tell which.
5. **Tolerance is the artist's: decided.** `repro.tolerance` is declared at build (`--tolerance N`, 0..64, refused outside), default 3 from the readings in §5; 0 is exact pixels and is asked for, never assumed. The verifier's `reproduced` means within tolerance on both axes inside the pinned engine; `exact` is reported beside it. The artist does not pick the number: they mark renders on a contact sheet (the reference, the device's GPU, another Chromium, a color shift) as the work or not, and the number is the largest accepted distance below the smallest rejected one; a rejected render at distance 0 or 1 means the hash does not see what they care about and that work declares a second measure. The sheet command is built (`dp1c sheet`); its mark-and-sign step is not. Open: whether 3 holds on works whose identity is color or fine texture; the correction rule is that a known-broken render passing at the default lowers it, and an intact work on a GPU failing at the default raises it, measured, not argued.
6. **Where engine images live.** IPFS plus Arweave for the blob; every FF1 ships the image for its own Chromium. Also the 2036 runtime: a tested emulator recipe (kernel, CPU model) is not yet part of the deliverable.
7. **Works that fetch: decided, the device's capture is the input.** `dp1c import-capture` reads FF OS's offline-artworks store (`items/<sha256(source)>.json` plus `blobs/`) and writes a replay capsule; nothing is re-crawled. Demonstrated on a WebGL work captured by a Kitchen FF1 (`examples/evolved-formula-22-1.dp1c`). Remaining: the export runs on a laptop today; on the device it is a function of feral-controld (below).

## How this fits the device

The device already captures everything it plays (feral-controld's offline cache runs each web work once in headless Chromium and stores every response it made, content-addressed). A capsule is that capture plus the engine pin plus a hash, written as one file. So the device can make one for every work it holds, check it against itself on a schedule, and the owner's deliberate act is export, not creation: put the capsule somewhere other than this device. The spec's §10 conservation loop is exactly this and was never built. Two asks for FF OS, both on code the team owns:

1. **Capture to capsule on the device.** A `feral-controld` function that turns an item record into a replay capsule (the mapping in `dp1c.py`'s `cmd_import_capture`, ~60 lines) and renders the reference frame with the device's own headless Chromium under the engine layer's flags, frame schedule and virtual-time settings. The engine pin on a device-made capsule names the FF OS release's Chromium (`version`) and the engine image published for that release (`imageRef`); the device does not need the image to build, only to verify elsewhere.
2. **The verify loop.** A timer that re-renders each held capsule and compares the frame hash: `preserved` or `drifted`, shown per work in the app, raised as `reproMismatch` per §10. Host-mode verification on the device is a different-engine comparison unless the device runs the pinned image; both results are useful and both are reported as what they are.
