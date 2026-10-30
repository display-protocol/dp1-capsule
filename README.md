# dp1c — a DP-1 preservation capsule that proves itself

Prototype, 2026-10-30. One Python file. Builds `*.dp1c` capsules per DP-1 v1.1.0 §9 and fills the two things the spec leaves as placeholders: the engine layer (`/meta/engine/<layer>`) and the `repro.frameHash` in §5, computed by a real headless render with the network unreachable.

## The claim it lets an owner test

A capsule is a file. Anyone with the file and a Chromium binary can run `verify` on a laptop with the network off and no Feral File anywhere, and get a yes or a no: the work rendered to the same pixels the publisher recorded. Trust by possession, not by promise.

## What it does

```
python3 dp1c.py build  WORKDIR --entry index.html --out work.dp1c [--title ..] [--source URL] [--provenance p.json] [--seed 0x..] [--created ISO]
python3 dp1c.py verify work.dp1c [--png out.png]
python3 dp1c.py info   work.dp1c
```

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

`examples/fidenza-503.dp1c`. HTML read straight from the Art Blocks on-chain generator (`getTokenHtml`, block 26308641), p5 1.0.0 inlined by the contract, zero network references. 1920×1080, builds in under a second, verifies exact three times out of three on this machine. Changing one nibble of the token hash in the HTML is caught twice: the asset hash fails and the frame hash fails (dHash distance 8, exit 2).

## What is real and what is not

Real: the layout, the deterministic archive, the offline render, the pixel hash, exact reproduction across runs on one machine, the tamper detection.

Not yet real, in order of how much they matter:

1. **Cross-machine reproduction.** Software raster + no-hinting fonts is the bet that pixels match across Linux boxes with the same Chromium build. Untested beyond this laptop. Fonts are the likely first break; the fix is to ship the fonts in `assets/` or pin a font set in the engine layer.
2. **Cross-version reproduction.** Chromium 155 today. In ten years `verify` will say `reproMismatch` because the engine moved, not the work. `imageRef` in the engine pin is left null on purpose: it is where a content-addressed runtime image (OCI digest, Nix closure, or a WASM-built browser) goes, so a future machine can run *this* engine. That is the preservation problem proper, and it is a packaging problem, not a research problem.
3. **Works that fetch.** Fidenza is the easy case. A work that pulls a library from a CDN, assets from IPFS, or data from an API needs a capture step first. FF OS already has that capture (`ffos-user/docs/offline-artwork-capture.md`) and its cache layout was designed to export to this format.
4. **First frame vs. time.** `frameHash` is the frame at the virtual-time budget. Animated and interactive works need a frame schedule, or a hash of frame N under fixed virtual time, which the same flags support.
5. **Signing.** The spec signs playlists; the capsule should be signed as a whole so the frame hash cannot be swapped with the asset.

## Why this and not a screenshot

A screenshot proves what the publisher saw. A capsule lets the owner re-derive it. The difference is who has to be trusted.
