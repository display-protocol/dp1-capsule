# DP-1 capsule: proposed text for §5, §9 and the engine layer

Draft for discussion against DP-1 v1.1.0. Written from a working prototype (`dp1c.py`, `engine/build-engine.sh`), not from intent.

## The claim a capsule makes

A capsule is a file that lets anyone, offline, with no party to the original sale present, re-derive the pixels the publisher recorded for a work. Two promises are kept separately and both are checkable:

1. **The work is intact.** Every asset is content-hashed; the playlist records the hashes.
2. **The work renders the same.** A pinned engine, run with the network unshared, produces a frame whose pixel hash matches the recorded `repro.frameHash`.

Art preserved on chain keeps promise 1 already. The capsule exists for promise 2.

## §9 Capsule format (replaces the current seven lines)

```
playlist.json                 DP-1 playlist, one or more items, each with repro + provenance
assets/                       the work: entry document and every file it loads
meta/engine/<engine>.json     the engine layer (below)
meta/render/reference.json    entry, per-asset sha256, frameHash, renderedAt
meta/render/reference.png     the reference frame
```

Archive: tar (USTAR, entries sorted by name, mtime 0, uid/gid 0, mode 0644) then zstd with no checksum frame. The capsule id is the sha256 of the archive bytes. Same inputs produce the same id; ids in `playlist.json` are uuid5 of source and asset hashes, not random.

## The engine layer

`meta/engine/chromium.json`:

```json
{
  "engine": "chromium",
  "version": "155.0.8059.84",
  "platform": "linux-x86_64",
  "renderer": "software",
  "flags": ["--headless=new", "--disable-gpu", "--hide-scrollbars", "--font-render-hinting=none",
            "--disable-lcd-text", "--run-all-compositor-stages-before-draw", "--deterministic-mode",
            "--enable-begin-frame-control", "--disable-extensions", "--disable-background-networking",
            "--no-first-run", "--no-default-browser-check", "--no-sandbox"],
  "viewport": {"width": 1920, "height": 1080, "deviceScaleFactor": 1},
  "virtualTimeBudgetMs": 15000,
  "network": "none",
  "imageRef": {
    "sha256": "<sha256 of the engine image>",
    "format": "tar+zstd rootfs",
    "source": "https://archive.archlinux.org/repos/2026/10/20/",
    "packages": {"chromium": "155.0.8059.84-1", "ttf-dejavu": "...", "...": "..."},
    "run": "bwrap --unshare-all --ro-bind <rootfs> / ... /usr/bin/chromium"
  }
}
```

**`imageRef` is the pin.** It names a content-addressed root filesystem that contains exactly this engine, its fonts, and the verify harness. The image is built from a dated package archive, so it can be rebuilt from `source` and `packages` if the blob itself is lost. One image serves every capsule built with it; the capsule carries the reference, not the image.

The image runs three ways, in order of expected lifetime: today under a user-namespace sandbox (bubblewrap, no root, no daemon); as an OCI container by import; and under full x86-64 emulation when neither exists. The third is the preservation path proper. x86-64 Linux is the most emulated computing platform there is; this is the same bet game preservation and web-archive emulation have made, and it has held for decades.

**Software raster for verification, GPU for display.** The engine renders with `--disable-gpu`, so pixels do not depend on a GPU, driver, or vendor. A player showing the work may use the GPU. The reference hash is a conservation artifact, not a display requirement.

**Network is unshared at the kernel.** The sandbox gets its own empty network namespace with loopback only; the work is served to the engine from inside it. A work that needs a host that is not in `assets/` fails to render, which is the correct answer: the capsule is incomplete.

## §5 Deterministic reproduction (amendments)

- `frameHash.sha256` is over decoded RGB pixels, not over the PNG container.
- `frameHash.phash` is a 64-bit difference hash; players report Hamming distance so a near miss is distinguishable from a wrong work.
- A verifying player **MUST** run inside the engine named by `imageRef` when it is available, and **MUST** say so when it is not. A result produced by a different engine is reported with both versions and is a weaker claim.
- Verdicts: `reproduced` (exact), `near` (distance ≤ 4, engine differs), `reproMismatch`.

## Open questions for the team

1. **Frame schedule.** `frameHash` is the frame at the virtual-time budget. Animated and interactive works need either a list of (virtualTimeMs, frameHash) pairs or an artist-declared settle time. Which, and who declares it.
2. **Tolerance is the artist's.** Exact pixels is the conservator's bar. The artist may accept a tolerance (anti-aliasing drift on edges). Proposal: `repro.tolerance` with a dHash distance, set by the artist or publisher, default 0.
3. **Capsule signature.** The spec signs playlists. The capsule should be signed whole so the frame hash cannot be swapped with the assets.
4. **Works that fetch.** FF OS's offline capture already records the resource list with hashes; its cache layout maps onto `assets/` directly. The capsule builder should take that capture as input rather than re-crawling.
5. **Where the engine images live.** IPFS plus Arweave mirror for the blob; the FF1 ships with the image for its own Chromium, so every installed device is a copy.
6. **Fonts.** Pinned by package in the image. Works that load web fonts are case 4.
