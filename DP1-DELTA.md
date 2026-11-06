# Proposed changes to DP-1 v1.1.0, ready for a pull request against the spec

Three deltas, each a small text change. The reasoning and the measurements are in `SPEC-PROPOSAL.md`; this file is only the text a reviewer of the public spec needs.

## 1. §5 Reproduction: a declared tolerance, two-axis distance, exact on request

Replace: "Players MUST compute the first-frame SHA-256; discrepancy triggers `reproMismatch`."

With: "`repro.tolerance` is an integer 0..64, the dHash distance within which a render is the work, declared by the artist or publisher; 0 means exact pixels, decided by `frameHash.sha256`. Absent, a player uses 3. A player computes the distance on two axes, the recorded row hash (`frameHash.phash`, 9×8, left > right) and a column hash of the same construction (8×9, upper > lower) computed from the reference frame it holds and from its render, and compares the larger with the tolerance. The frame compared is the one captured after the engine layer's settle budget and frame schedule, not the first frame. A render over the tolerance, or at tolerance 0 any pixel different, is `reproMismatch`; a render that loaded something that failed, threw, or ran outside the engine's clock is `renderProblems` whatever its pixels."

Keep: "If an artwork requires a derived seed, the logic MUST be contained within the artwork payload; DP-1 does not standardize seedScript execution at this time." Add: "A capsule may carry the seed a work takes from its URL as part of `entry`, which makes it part of the payload."

## 2. §7 Signatures: the artist's sign-off

Add after the signature object shape: "A capsule's detached signature covers `sha256:<capsule id>`. A signature with `role` `artist` is the artist's sign-off: this frame, in this engine, at this time, within this tolerance, is the work. A second form lets an artist sign with the key they already hold: `alg` `eip191`, `kid` `did:pkh:eip155:<chain>:<address>`, `sig` the wallet's `personal_sign` over the same string, verified by recovering the address. A later signature by the same artist is a new witness, not a correction of the earlier one."

## 3. New section, Sources: public schemes only

Add: "A player resolves an item's `source` by its URI scheme and by nothing else. The schemes are public standards, each implemented once: `ipfs://` and `ar://` (content-addressed, the bytes verified against the address), `onchfs://` (content-addressed, every inode verified against its address), `web3://` (ERC-4804, the work is the return of a contract call, trusted through a node), `tezos-storage:` (TZIP-16), and `data:` (the work travels inside the signed playlist). An `https://` source is a host to reach, and a player reads the item's `medium.type` before probing that host for a type. Anything particular to one artist belongs in that artist's contract, never in the player. A player MUST NOT depend on a gateway table: a gateway is one host for a scheme, and a dead gateway must not make a content-addressed work unreachable."

## What stays as proposal, not spec text

The engine layer (`/meta/engine/<layer>`, the image pin, the frame clock) and the capsule layout are in `SPEC-PROPOSAL.md` as §9 text and are larger; they can follow once the team has read them.
