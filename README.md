# dp1c — a DP-1 preservation capsule that proves itself

Prototype, 2026-10-30. Two Python files and an engine builder. Builds `*.dp1c` capsules per DP-1 v1.1.0 §9 and fills what the spec leaves blank: the engine layer (`/meta/engine/<layer>`, with a content-addressed `imageRef`) and the `repro.frameHash` in §5, computed by a harness that drives Chromium over the DevTools protocol inside a pinned engine image, with the network namespace unshared, the clock pinned and the viewport set before navigation. The proposed spec text and the decisions it leaves to the team are in `SPEC-PROPOSAL.md`.

Reviewed read-only by Codex (gpt-6-astra) and Grok on 2026-10-30, both "revise"; this version answers their blockers (see "What changed after review").

## The claim it lets an owner test

A capsule is a file. Anyone with the file, the engine image it names, and a Linux machine with bubblewrap can run `verify` offline, with no Feral File service anywhere, and get a yes or a no: the work's files are exactly what the publisher packed, and the work rendered to the same pixels the publisher recorded, inside the same engine. Without the image, `--host` compares against this machine's Chromium instead; that result is reported as a different-engine comparison, never as `reproduced`.

## What it does

```
engine/build-engine.sh 2026/10/20 out/ [exact-chromium-pkg-url]        # once per engine: content-addressed rootfs + manifest
python3 dp1c.py build  WORKDIR --engine out/engine-<sha>.tar.zst --out work.dp1c --budget 15000 [--entry index.html] [--title ..] [--source URL] [--provenance p.json] [--seed 0x..] [--created ISO]
python3 dp1c.py verify work.dp1c [--engine out/engine-<sha>.tar.zst] [--host] [--allow-engine-mismatch] [--png out.png]
python3 dp1c.py info   work.dp1c
python3 dp1c.py keygen key.pem && python3 dp1c.py sign work.dp1c --key key.pem --role publisher
./verify-in-docker.sh out/engine-<sha>.tar.zst work.dp1c [work.dp1c.sig.json]     # on a machine with Docker instead of bubblewrap (untested as of 2026-10-30)
```

`build` copies the work into `assets/`, hashes every file, renders once inside the engine, refuses to write a capsule if any resource failed to load, any HTTP error other than favicon occurred, or an uncaught exception fired (`--allow-render-errors` records them instead), writes the pin, the reference frame and its hash, and packs a deterministic archive whose sha256 is the capsule id. Same inputs and the same `--created` give the same bytes.

`sign` writes a detached Ed25519 signature over the capsule id into `<capsule>.sig.json`, in the DP-1 §7 object shape with a `did:key` identifier (`keygen` makes a key). `verify --require-signer <did:key>` refuses a capsule not validly signed by that key; forged signatures and signatures carried over to a tampered capsule both fail.

`verify` checks three things and reports them separately: every file under `assets/` matches the manifest and the playlist (no missing, no extra, no changed); the three copies of the reference frame agree (playlist, `reference.json`, the PNG's own pixels); and a fresh render inside the pinned engine, under the pinned harness, reproduces the frame hash. Verdicts: `reproduced` (exit 0), `reproducedOnDifferentEngine`, `nearOnDifferentEngine`, `reproMismatch`, `assetsCorrupt` (all exit 2). Flags and environment for the render come from this tool, never from the capsule.

### The engine image

`engine/build-engine.sh` builds a root filesystem from the Arch Linux package archive at a dated snapshot, optionally with one exact Chromium package installed over it, with no root and no daemon (pacman inside `unshare -r`). It holds Chromium, the fonts FF OS ships (DejaVu, Liberation, Noto CJK) plus whatever GTK pulls in (Adwaita), Python with Pillow and zstandard, and util-linux for `setpriv`. Package install scriptlets do run; afterwards the machine id is emptied and install dates are stripped from the package database. The tree is packed as a deterministic tar (sorted, mtime 0, numeric owner 0) and single-threaded zstd 19 with no checksum frame; the sha256 of that file is the engine id. The sidecar `engine-<sha>.json` records every package version, the sha256 of every package file, the archive URL, the exact Chromium package URL and the host build tools. About 575 MB compressed, 1.7 GB unpacked; one image serves every capsule built with it.

### The sandbox

Both build and verify run `harness.py` under bubblewrap: every namespace unshared; the image read-only at `/` (or, with `--host`, this machine's root filesystem); `assets/` read-only at `/work`; one fresh directory writable at `/out`; the harness read-only at `/harness`; environment cleared and set to the pin's `env`; cwd `/`. The harness brings loopback up inside the empty network namespace (a Python ioctl, needs CAP_NET_ADMIN), then starts Chromium through `setpriv` with the bounding set emptied and no-new-privs set, so the browser runs with no capabilities at all. Measured: `CapEff 0`, `CapBnd 0` in the browser process. A work that needs a host outside `assets/` fails with `net::ERR_INTERNET_DISCONNECTED` from the kernel, and build refuses.

The harness talks to Chromium over the DevTools pipe: device metrics set before navigation (the page sees the pinned viewport from its first frame), timezone and locale overrides, cache disabled, virtual time paused at a fixed epoch before navigation, a fixed budget that advances only while no fetches are pending, screenshot at budget expiry. The page's `Date.now()` at capture is deterministic (measured: `1600000015000` for a 15 s budget). `Math.random` and `crypto.getRandomValues` are left alone by default, because on-chain generative art seeds itself from the token hash (§5 requires the seed logic to live in the payload). For a work that does not, `build --pin-random <seed>` has the harness install a seeded generator before any page script runs; the work is untouched, the seed is recorded in the pin, and the capsule then certifies one witnessed run of the system. Measured: a test page drawing from `Math.random` gave different frames on two unpinned builds and identical frames and capsule ids on two pinned ones.

## Worked example: Fidenza #503

`examples/fidenza-503.dp1c`. HTML read straight from the Art Blocks on-chain generator (`getTokenHtml`, block 26308641), p5 1.0.0 inlined by the contract, zero network references. 1920×1080, 15 s virtual budget. Measured on this machine:

- Same inputs, two builds: same capsule id.
- Inside the pinned engine, verify: `reproduced`, exit 0.
- Host Chromium 155 (same version, different build and environment) via `--host` with the image hidden: same frame hash, reported as `reproducedOnDifferentEngine`, exit 2.
- Chromium 154 image via `--allow-engine-mismatch`: same frame hash, reported as `reproducedOnDifferentEngine`. One version step, zero pixel drift, for this work.
- Tampers caught: one nibble of the token hash (assets and pixels both fail); a comment-only edit (pixels same, assets fail); an extra unlisted file; an edited `reference.json` frame record. Wrong engine image refused. Missing engine refused without `--host`.
- A test page loading p5 from a CDN is refused at build with the kernel's disconnected error.

## What changed after review

Codex and Grok each found the same five blockers in the first version and a long tail. Fixed here: verify checked only a screenshot's existence (now the harness's exit, both output files, and the frame geometry are required, and output goes to a fresh directory outside the capsule); the asset check covered only listed files (now the whole tree, both directions, and the playlist's list); the engine pin was bypassed by cached or directory engines (now the computed digest is the only thing trusted, cache entries carry a completion stamp, and a mismatch refuses unless explicitly allowed and is then reported as a different engine); capsule fields reached `bash -c` (now the harness reads a JSON config on stdin and the entry is URL-encoded); the host path only blocked DNS (now it runs under bubblewrap with the network namespace unshared too); a missing dependency rendered a blank frame as success (now load failures fail the build); the pixel hash ignored geometry (now width, height and format are in the hash); the engine id depended on multithreaded zstd and install-time state (now single-threaded, machine id and install dates normalized); the viewport and clock were not pinned (now set over DevTools before navigation); the harness copy in the image was not the one that ran (now the harness that runs is bind-mounted and its sha256 is in the pin).

## What is real and what is not

Real: the layout, the deterministic archive, the pinned engine image and its builder, the kernel-level offline render inside it with capabilities dropped, the DevTools-driven capture with pinned clock and viewport, the three-way asset and frame checks, exact reproduction across runs and across one Chromium version step, the refusals.

Not yet real, in order of how much they matter:

1. **Cross-machine reproduction.** The image removes the GPU, fonts, locale and clock; it has only run on this laptop. SwiftShader and Skia may still take CPU-specific paths. The claim to test next: same image, different machine, same hash.
2. **Bit-exact engine rebuild.** Install scriptlets run during the build and generate caches. The manifest now carries every package file's sha256, so a rebuild can be checked against the image digest, but a matching rebuild has not yet been demonstrated. The blob on IPFS plus Arweave is the real pin; the recipe is the fallback.
3. **The engine in 2036.** The image runs under bubblewrap today and would import as an OCI container. Running it under x86-64 emulation, with a pinned kernel and CPU model, is the long-term path and has not been exercised.
4. **Who signs.** The example is signed by a prototype key generated on this laptop. Which key signs in production and where its `did:key` is published is open.
5. **Works that fetch.** Fidenza is the easy case. FF OS's offline capture already records the resource list with hashes; its cache layout maps onto `assets/`.
6. **Frame schedule and tolerance.** `frameHash` is the frame at the budget. Animated works need a schedule; the artist may want a tolerance. Decisions 4 and 5.

## Why this and not a screenshot

A screenshot proves what the publisher saw. A capsule lets the owner re-derive it, inside the same engine, with nothing else reachable. The difference is what has to be trusted: the file and the image, not the publisher.
