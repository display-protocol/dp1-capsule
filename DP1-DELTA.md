# Proposed changes to DP-1 v1.1.0, ready for a pull request against the spec

Three deltas. The reasoning and the measurements are in `SPEC-PROPOSAL.md`; this file is the text a reviewer of the public spec needs, with what the prototype demonstrates and what it does not stated beside each.

## 1. §5 Reproduction: a declared tolerance, two-axis distance, exact on request

Replace: "Players MUST compute the first-frame SHA-256; discrepancy triggers `reproMismatch`."

With:

- "`frameHash.sha256` is sha256 over the ASCII prefix `"<width>x<height>:RGB8:"` followed by the decoded 8-bit RGB pixels in row-major order, of the frame captured after the engine layer's settle budget and frame schedule (§9, the engine layer). It is not the first frame."
- "`frameHash.phash` is a 64-bit difference hash: convert to 8-bit grayscale, resize to 9×8 with Lanczos, for each of the 8 rows emit 8 bits left to right where bit = left pixel > right pixel, pack row-major big-endian, hex with `0x`."
- "`repro.tolerance` is an integer 0..64, the dHash distance within which a render is the work, declared by the artist or publisher; 0 means exact pixels, decided by `frameHash.sha256`. Absent, a player uses 3."
- "A player computes the distance on two axes: the recorded row hash, and a column hash of the same construction (resize to 8×9, bit = upper pixel > lower pixel) computed from the reference frame it holds (`meta/render/reference.png` in a capsule) and from its render. The larger of the two distances is compared with the tolerance."
- "Verdicts, in order: `assetsCorrupt` (the asset manifest, the three copies of the reference frame, or the engine pin disagree; nothing is rendered); `renderProblems` (the render loaded something that failed or returned an HTTP error, threw, missed the capture table, left a request in flight at capture, or ran a worker or out-of-process frame; or the capsule's build recorded any of these; pixels are compared and reported but do not decide); `reproMismatch` (the larger distance is over the tolerance, or at tolerance 0 any pixel differs); `reproduced`."

Keep: "If an artwork requires a derived seed, the logic MUST be contained within the artwork payload; DP-1 does not standardize seedScript execution at this time." Add: "A capsule may carry the seed a work takes from its URL as part of `entry`, which makes it part of the payload."

Demonstrated by the prototype: all of the above, on five example capsules and a test suite. Depends on §9's engine layer and reference frame, which are proposed in `SPEC-PROPOSAL.md` and are the larger change.

## 2. §7 Signatures: the artist's sign-off

Add after the signature object shape:

- "A capsule's detached signature covers `sha256:<capsule id>`, so it covers the frame, the engine pin and the tolerance together. A playlist inside a capsule is signed by the capsule signature and is exempt from §7.1.3's rejection rule when played from a verified capsule."
- "A signature with `role` `artist` is the artist's sign-off: this frame, in this engine, at this time, within this tolerance, is the work. The role is the signer's claim; what makes the sign-off the artist's is `kid` being the artist's published key (the wallet that deployed the work's contract, or a key the artist has published), which a player checks against what it knows of the artist, not against the role field."
- "A second signature form lets an artist sign with the key they already hold: `alg` `eip191`, `kid` `did:pkh:eip155:<chain>:<address>`, `sig` the wallet's `personal_sign` over the same string, verified by recovering the address."
- "A later signature by the same key is a new witness beside the earlier one, not a replacement."

Demonstrated: Ed25519 signing and verification, with and without a library; a later signature by the same key is kept beside the earlier one. Not built: the `eip191` form (the verifier rejects every algorithm but Ed25519 today), and any check of a `kid` against an artist's published key.

## 3. New section, Sources: public schemes only

Add:

- "A player resolves an item's `source` by its URI scheme and by nothing else. The schemes are public standards, each implemented once: `ipfs://` and `ar://` (content-addressed, the bytes verified against the address), `onchfs://` (content-addressed, every inode verified against its address), `web3://` (ERC-4804, the work is the return of a contract call, trusted through a node), `tezos-storage:` (TZIP-16), and `data:` (the work travels inside the signed playlist)."
- "An `https://` source is a host to reach. A player reads the item's `medium.type` before probing that host for a type."
- "Anything particular to one artist belongs in that artist's contract, never in the player. A player MUST NOT depend on a gateway table: a gateway is one host for a scheme, and a dead gateway must not make a content-addressed work unreachable."

Demonstrated: readers for `onchfs://` (every inode verified) and `web3://` (ERC-4804 auto mode), as separate tools in this repo, on three works that read straight from chain with no gateway. Not built: a general resolver that dispatches on scheme inside `build`, and any player reading `medium.type` first. The rule is written from the failures (`feral-file#3599`), not from a finished implementation.

## What stays as proposal, not spec text

The engine layer (`/meta/engine/<layer>`, the image pin, the frame clock) and the capsule layout are in `SPEC-PROPOSAL.md` as §9 text; they can follow once the team has read them.
