# DP-1 capsule: proposed text for §5, §9 and the engine layer

Draft for discussion against DP-1 v1.1.0, written from a working prototype (`dp1c.py`, `harness.py`, `engine/build-engine.sh`). Reviewed read-only by Codex (gpt-6-astra) and Grok on 2026-10-30; the revisions below answer their findings. Open decisions for the team are at the end.

## The claim a capsule makes

A capsule is a file that lets anyone, offline, with no party to the original sale present, re-derive the pixels the publisher recorded for a work. Two promises are kept separately and both are checked:

1. **The work is intact.** Every regular file under `assets/` is content-hashed; the manifest must cover the tree exactly (no unlisted files, no missing files); the playlist's `repro.assetsSHA256` must agree with the manifest.
2. **The work renders the same.** The pinned engine, run with the network namespace unshared, the clock pinned, and the viewport set before navigation, produces a frame whose pixel hash equals the recorded `repro.frameHash`.

Art preserved on chain keeps promise 1 already. The capsule exists for promise 2.

## What v1.1.0 says today (quoted, so the delta is reviewable)

§5: "`repro`: engineVersion, seed, assetsSHA256, frameHash {sha256, phash}. Players MUST compute the first-frame SHA-256; discrepancy triggers `reproMismatch`. If an artwork requires a derived seed, the logic MUST be contained within the artwork payload; DP-1 does not standardize seedScript execution at this time."

§9: "`<playlist>.json` / `/assets/<…>` / `/meta/engine/<layer>`. Tar + Zstd; SHA-256 of archive = capsule CID."

## §9 Capsule format (replaces the quoted text)

```
playlist.json                 DP-1 playlist, exactly one item (see decision 3), with repro and provenance blocks
assets/                       the work: entry document and every file it loads, nothing else
meta/engine/chromium.json     the engine layer (below)
meta/render/reference.json    entry, asset manifest {relpath: sha256}, frame record, page probe, render problems
meta/render/reference.png     the reference frame
```

**Archive.** USTAR tar with regular-file entries only, no directory entries, paths sorted bytewise, mtime 0, uid/gid 0, empty uname/gname, mode 0644; then zstd level 19, single frame, content size written, no checksum frame. The capsule id is the sha256 of the archive bytes. All JSON files are canonical: keys sorted, separators `,` and `:`, UTF-8, no trailing newline. Playlist and item ids are UUID v5 over the URL namespace of `dp1c:playlist:<fp>` and `dp1c:item:<fp>`, where `<fp>` is `<source>|<relpath>=<sha256>|…` over the sorted manifest. Same inputs, same bytes, same id. (Measured: two builds of the Fidenza example produce identical files.)

**Entry.** `reference.json.entry` is a plain file name at the asset root. Players serve `assets/` over loopback HTTP from inside the sandbox and navigate to `http://127.0.0.1:<port>/<entry>`.

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
  "virtualEpochSeconds": 1600000000, "virtualTimeBudgetMs": 15000, "timezone": "UTC", "locale": "en-US",
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

**The procedure is pinned too.** `harnessSha256` is the digest of the harness that drove the browser. The harness runs inside the image; its DevTools commands define the capture: device metrics before navigation, timezone and locale overrides, cache disabled, virtual time paused at `virtualEpochSeconds` before navigation, then a budget of `virtualTimeBudgetMs` that advances only while the page has no network fetches pending, then `Page.captureScreenshot`. The page's `Date.now()` at capture is `virtualEpochSeconds * 1000 + virtualTimeBudgetMs`. `Math.random` and `crypto.getRandomValues` are left to the work by default (§5: seed logic lives in the payload). When the engine layer carries `randomSeed`, the harness installs a seeded generator (mulberry32 over the 32-bit seed) in place of both before any page script runs, and the frame then certifies one witnessed run. The work is never modified.

**Isolation.** The image runs under a user-namespace sandbox (bubblewrap): every namespace unshared, image read-only at `/`, `assets/` read-only at `/work`, one fresh writable directory at `/out`, the harness read-only at `/harness`, environment cleared and set to `env`, cwd `/`. Loopback is brought up inside the empty network namespace by the harness (needs CAP_NET_ADMIN), then Chromium is started through `setpriv` with the bounding set empty and no-new-privs set. Nothing outside the namespace is reachable over the network; a resource not in `assets/` fails with the kernel's own `ERR_INTERNET_DISCONNECTED`, and the build refuses to write the capsule.

**Software raster for verification, GPU for display.** The engine renders with SwiftShader (named explicitly, not by fallback), sRGB forced, Skia runtime optimizations off. A player showing the work may use the GPU. The reference hash is a conservation artifact, not a display requirement. CPU-dependent paths inside SwiftShader remain a cross-machine risk until measured.

## §5 Deterministic reproduction (replaces "Players MUST compute the first-frame SHA-256")

- `frameHash.sha256` is sha256 over the ASCII prefix `"<width>x<height>:RGB8:"` followed by the decoded 8-bit RGB pixels in row-major order, of the frame captured when the virtual-time budget expires. It is not the first frame.
- `frameHash.phash` is a 64-bit difference hash: convert to 8-bit grayscale, resize to 9×8 with Lanczos, for each of the 8 rows emit 8 bits left to right where bit = left pixel > right pixel, pack row-major big-endian, hex with `0x`. Players report Hamming distance.
- `assetsSHA256` lists the manifest's hashes sorted by relative path.
- A verifying player MUST run inside the engine named by `imageRef`. Verdicts: `reproduced` (exact, same image, same harness digest); `reproducedOnDifferentEngine`; `nearOnDifferentEngine` (distance ≤ 4); `reproMismatch`; `assetsCorrupt` (promise 1 failed, reported before promise 2). Only `reproduced` exits 0.

## Decisions for the team

1. **Signing: decided, whole capsule.** A detached sidecar `<capsule>.dp1c.sig.json` carries `capsuleId` and a `signatures` array in the §7 object shape (`alg`, `kid` as `did:key`, `ts`, `payload_hash`, `role`, `sig`), where `payload_hash` is `sha256:<capsule id>` and `sig` is Ed25519 over that string, exactly as §7 signs a playlist's payload hash. The signature covers every byte of the capsule, so the frame record and the assets cannot be swapped independently. A verifier given `--require-signer <did:key>` refuses anything not validly signed by that key. The playlist inside stays unsigned; §9 should say a capsule's playlist is signed by the capsule signature and is exempt from §7.1.3's rejection rule when played from a verified capsule. Remaining: whose key signs in production (Feral File as publisher, the artist as a second signature), and where the kid is published.
2. **UUID version.** Content-derived UUID v5 ids give reproducible capsules; the Go validator and the schema text expect v4. Recommendation: schema says "UUID", validator accepts v4 and v5.
3. **Items.** One item per capsule, or a per-item reference record. Recommendation: one, until a real multi-item case exists.
4. **Frame schedule.** Randomness is handled (the work seeds itself, or `randomSeed` pins it). What remains is time: a work that keeps moving has no single frame, so animated works need a list of (virtualTimeMs, frameHash) or an artist-declared settle time, and interactive works need declared inputs. Who declares it.
5. **Tolerance is the artist's.** Exact pixels is the conservator's bar; the artist may name a dHash distance. Proposal: `repro.tolerance`, default 0.
6. **Where engine images live.** IPFS plus Arweave for the blob; every FF1 ships the image for its own Chromium. Also the 2036 runtime: a tested emulator recipe (kernel, CPU model) is not yet part of the deliverable.
7. **Works that fetch.** FF OS's offline capture already records resources with hashes; the capsule builder should consume it rather than re-crawl.
