# dp1c — a DP-1 preservation capsule that proves itself

Prototype, 2026-10-30. One Python file plus an engine builder. Builds `*.dp1c` capsules per DP-1 v1.1.0 §9 and fills the two things the spec leaves as placeholders: the engine layer (`/meta/engine/<layer>`, now with a content-addressed `imageRef`) and the `repro.frameHash` in §5, computed by a real headless render inside a pinned engine with the network unshared at the kernel. The proposed spec text is in `SPEC-PROPOSAL.md`.

## The claim it lets an owner test

A capsule is a file. Anyone with the file and a Chromium binary can run `verify` on a laptop with the network off and no Feral File anywhere, and get a yes or a no: the work rendered to the same pixels the publisher recorded. Trust by possession, not by promise.

## What it does

```
engine/build-engine.sh 2026/10/20 out/ [exact-chromium-pkg-url]      # once per engine: content-addressed rootfs
python3 dp1c.py build  WORKDIR --engine out/engine-<sha>.tar.zst --out work.dp1c [--title ..] [--source URL] [--provenance p.json] [--seed 0x..] [--created ISO]
python3 dp1c.py verify work.dp1c [--engine out/engine-<sha>.tar.zst] [--host] [--png out.png]
python3 dp1c.py info   work.dp1c
```

### The engine image

`engine/build-engine.sh` builds a root filesystem from the Arch Linux package archive at a dated snapshot, with no root and no daemon (pacman inside `unshare -r`). It holds Chromium, the FF OS font set (DejaVu, Liberation, Noto CJK), Python with Pillow and zstandard, and this tool. Install hooks are skipped and the two that matter (ldconfig, fc-cache) run by hand. The tree is packed as a deterministic tar (sorted, mtime 0, numeric owner 0) plus zstd 19 with no checksum frame; the sha256 of that file is the engine id. A sidecar `engine-<sha>.json` records every package and version and the archive URL, so the image can be rebuilt if the blob is lost. About 575 MB compressed, 1.7 GB unpacked; one image serves every capsule built with it.

The render runs inside the image under bubblewrap: `--unshare-all`, uid 0 inside the namespace, a cleared environment with fixed HOME, TMPDIR, XDG dirs, LANG and TZ, loopback brought up by a Python ioctl (no iproute2 needed), the work served over 127.0.0.1 from inside the namespace, `/work` read-only, `/out` the only writable bind. Nothing outside the namespace is reachable.

`verify` resolves the engine from the capsule's `imageRef` (cache at `~/.cache/dp1c/engines/<sha>/`), or from `--engine`, which is checked against the pin. If the capsule is pinned and the engine is absent, verify refuses unless `--host` is passed, and then reports `ranInsideEngine: false` with both versions.

`build`:
1. copies the work's asset tree into `assets/`, sha256 of every file
2. writes the engine pin to `meta/engine/chromium.json`: Chromium version, platform, software raster, the full flag list, viewport, virtual-time budget, `network: none`
3. renders the entry once through a loopback HTTP server with every other host mapped to NOTFOUND (`--host-resolver-rules`), software rendering, deterministic mode, fixed virtual time
4. hashes the decoded pixels (not the PNG container) and a 64-bit dHash, writes them into `playlist.json` → `items[0].repro.frameHash`, keeps the reference PNG at `meta/render/reference.png`
5. packs a deterministic tar (sorted, mtime 0, uid 0) + zstd 19 (no checksum frame). sha256 of the archive is the capsule id; same work + same `--created` ⇒ same bytes, same id.

`verify` extracts, checks every asset hash, re-renders under the capsule's own engine pin with the network blocked, and compares. Exit 0 = reproduced, 2 = mismatch. Output names the engine the capsule was pinned to and the engine on this machine so a drift can be attributed.

## Layout

```
playlist.json                      DP-1 1.1.0, one item, repro + provenance blocks
assets/<entry and everything it needs>
meta/engine/chromium.json          the engine layer
meta/render/reference.json         entry, asset hashes, frameHash, renderedAt
meta/render/reference.png          the reference frame
```

Ids (playlist, item) are uuid5 of source + asset hashes, so they are stable across builds.

## Worked example: Fidenza #503

`examples/fidenza-503.dp1c`. HTML read straight from the Art Blocks on-chain generator (`getTokenHtml`, block 26308641), p5 1.0.0 inlined by the contract, zero network references. 1920×1080. Measured on this machine:

- Host Chromium 155.0.8059.84, three runs: identical frame hash `89ca81d5…5be4`.
- Inside the pinned engine image (Chromium 154.0.8037.97 from the 2026/10/20 snapshot), sandboxed, no network: the same frame hash `89ca81d5…5be4`. One version step, zero pixel drift, for this work.
- Same inputs, two builds: same capsule id.
- Changing one nibble of the token hash in the HTML is caught twice: the asset hash fails and the frame hash fails (dHash distance 8, exit 2).

## What is real and what is not

Real: the layout, the deterministic archive, the pinned engine image and its builder, the kernel-level offline render inside it, the pixel hash, exact reproduction across runs and across one Chromium version step, the tamper detection, the refusal to verify with an unpinned engine.

Not yet real, in order of how much they matter:

1. **Cross-machine reproduction.** The engine image removes the GPU, the fonts, the locale and the clock from the equation; it has only been run on this laptop. The claim to test next is: same image, different machine, same hash.
2. **The engine in 2036.** The image runs under bubblewrap today and would import as an OCI container. Running it under x86-64 emulation when neither exists is the long-term path and has not been exercised. Where the image blob lives (IPFS plus Arweave, and every FF1 that ships with it) is a decision, not code.
3. **Works that fetch.** Fidenza is the easy case. A work that pulls a library from a CDN, assets from IPFS, or data from an API needs a capture step first. FF OS already has that capture (`ffos-user/docs/offline-artwork-capture.md`) and its cache layout was designed to export to this format.
4. **First frame vs. time.** `frameHash` is the frame at the virtual-time budget. Animated and interactive works need a frame schedule, or a hash of frame N under fixed virtual time, which the same flags support.
5. **Signing.** The spec signs playlists; the capsule should be signed as a whole so the frame hash cannot be swapped with the asset.

## Why this and not a screenshot

A screenshot proves what the publisher saw. A capsule lets the owner re-derive it. The difference is who has to be trusted.
