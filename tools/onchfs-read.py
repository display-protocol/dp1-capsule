#!/usr/bin/env python3
"""Read an onchfs:// tree straight from the on-chain file system contract and write it to a directory.

  tools/onchfs-read.py <cid 64 hex> <outdir> [--chain eip155:1] [--block N]

onchfs (fxhash's on-chain file system) stores a directory as (names, inode checksums) and a file as chunk
checksums plus HPACK-encoded headers. The two view functions used here, readDirectory(bytes32) and
readFile(bytes32), return the names and checksums of a directory and the concatenated bytes of a file.
Every read is made at one pinned block, recorded in <outdir>/onchfs-read.json with the sha256 of each
file as stored and as written. Files that arrive gzip-compressed (the content-encoding onchfs writers use;
sniffed by the gzip magic, the HPACK headers are not decoded here) are written decompressed, so a browser
can serve them from a plain directory. Keyless public RPCs; no fxhash service is involved, which matters
because fxhash closed on 2026-08-17 and its gateway no longer resolves.
"""

import argparse, gzip, hashlib, json, os, sys, urllib.request

CONTRACTS = {  # from onchfs-js 0.1.0 (DEFAULT_CONTRACTS)
    "eip155:1": "0x9e0f2864c6f125bbf599df6ca6e6c3774c5b2e04",
    "eip155:8453": "0x2983008f292a43f208bba0275afd7e9b3d39af3b",
    "eip155:11155111": "0x4f555d39e89f6d768f75831d610b3940fa94c6b1",
    "eip155:84532": "0x3fb48e03291b2490f939c961a1ad088437129f71",
}
RPCS = {
    "eip155:1": [
        "https://ethereum-rpc.publicnode.com",
        "https://eth-mainnet.public.blastapi.io",
        "https://1rpc.io/eth",
        "https://rpc.mevblocker.io",
    ],
    "eip155:8453": ["https://base-rpc.publicnode.com", "https://mainnet.base.org"],
    "eip155:11155111": ["https://ethereum-sepolia-rpc.publicnode.com"],
    "eip155:84532": ["https://base-sepolia-rpc.publicnode.com"],
}

# keccak-256 in plain Python (only the two function selectors need it; checked against the empty-string vector)
_RC = [
    0x0000000000000001,
    0x0000000000008082,
    0x800000000000808A,
    0x8000000080008000,
    0x000000000000808B,
    0x0000000080000001,
    0x8000000080008081,
    0x8000000000008009,
    0x000000000000008A,
    0x0000000000000088,
    0x0000000080008009,
    0x000000008000000A,
    0x000000008000808B,
    0x800000000000008B,
    0x8000000000008089,
    0x8000000000008003,
    0x8000000000008002,
    0x8000000000000080,
    0x000000000000800A,
    0x800000008000000A,
    0x8000000080008081,
    0x8000000000008080,
    0x0000000080000001,
    0x8000000080008008,
]
_ROT = [[0, 36, 3, 41, 18], [1, 44, 10, 45, 2], [62, 6, 43, 15, 61], [28, 55, 25, 21, 56], [27, 20, 39, 8, 14]]
_M = (1 << 64) - 1


def _rol(x, n):
    n %= 64
    return ((x << n) | (x >> (64 - n))) & _M if n else x


def _keccak_f(A):
    for rc in _RC:
        C = [A[x][0] ^ A[x][1] ^ A[x][2] ^ A[x][3] ^ A[x][4] for x in range(5)]
        D = [C[(x - 1) % 5] ^ _rol(C[(x + 1) % 5], 1) for x in range(5)]
        A = [[A[x][y] ^ D[x] for y in range(5)] for x in range(5)]
        B = [[0] * 5 for _ in range(5)]
        for x in range(5):
            for y in range(5):
                B[y][(2 * x + 3 * y) % 5] = _rol(A[x][y], _ROT[x][y])
        A = [[B[x][y] ^ ((~B[(x + 1) % 5][y]) & B[(x + 2) % 5][y]) for y in range(5)] for x in range(5)]
        A[0][0] ^= rc
    return A


def keccak256(data):
    rate = 136
    A = [[0] * 5 for _ in range(5)]
    data = bytearray(data)
    data.append(0x01)
    while len(data) % rate:
        data.append(0)
    data[-1] |= 0x80
    for off in range(0, len(data), rate):
        block = data[off : off + rate]
        for i in range(rate // 8):
            A[i % 5][i // 5] ^= int.from_bytes(block[8 * i : 8 * i + 8], "little")
        A = _keccak_f(A)
    return b"".join(A[i % 5][i // 5].to_bytes(8, "little") for i in range(4))[:32]


assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"


class Chain:
    def __init__(self, chain, block=None):
        self.contract = CONTRACTS[chain]
        self.rpcs = RPCS[chain]
        self.block = block if block is not None else self.rpc("eth_blockNumber", [])
        if isinstance(self.block, str):
            self.block = int(self.block, 16)

    def rpc(self, method, params):
        err = None
        for rpc in self.rpcs:
            try:
                req = urllib.request.Request(
                    rpc,
                    data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
                    headers={"Content-Type": "application/json", "User-Agent": "dp1c-onchfs-read"},
                )
                r = json.load(urllib.request.urlopen(req, timeout=60))
                if "result" in r:
                    return r["result"]
                err = r.get("error")
                if isinstance(err, dict) and err.get("code") == 3:
                    return None  # execution reverted: not an inode of that kind
            except Exception as e:  # noqa: BLE001
                err = str(e)
        sys.exit(f"every RPC failed: {err}")

    def call(self, data):
        r = self.rpc("eth_call", [{"to": self.contract, "data": "0x" + data}, hex(self.block)])
        return None if r is None else bytes.fromhex(r[2:])


def sel(sig):
    return keccak256(sig.encode())[:4].hex()


def word(b, i):
    return int.from_bytes(b[32 * i : 32 * i + 32], "big")


def dyn_bytes(b, off):
    ln = word(b, off // 32)
    return b[off + 32 : off + 32 + ln]


def decode_directory(b):
    o1, o2 = word(b, 0), word(b, 1)
    n = word(b, o1 // 32)
    names = [dyn_bytes(b, o1 + 32 + word(b, (o1 + 32) // 32 + i)).decode() for i in range(n)]
    m = word(b, o2 // 32)
    sums = [b[o2 + 32 + 32 * i : o2 + 64 + 32 * i].hex() for i in range(m)]
    if n != m:
        sys.exit("directory names and checksums disagree")
    return list(zip(names, sums))


def walk(chain, cid, out, rel, manifest):
    r = chain.call(sel("readDirectory(bytes32)") + cid)
    if r is not None:
        os.makedirs(out, exist_ok=True)
        for name, sub in decode_directory(r):
            if "/" in name or name in (".", "..") or name.startswith("."):
                sys.exit(f"refusing directory entry {name!r}")
            walk(chain, sub, os.path.join(out, name), f"{rel}/{name}", manifest)
        return
    r = chain.call(sel("readFile(bytes32)") + cid)
    if r is None:
        sys.exit(f"{rel}: inode {cid} is neither a directory nor a file at block {chain.block}")
    stored = dyn_bytes(r, word(r, 0))
    plain = gzip.decompress(stored) if stored[:2] == b"\x1f\x8b" else stored
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    with open(out, "wb") as f:
        f.write(plain)
    manifest.append(
        {
            "path": rel.lstrip("/"),
            "inode": cid,
            "storedBytes": len(stored),
            "storedSha256": hashlib.sha256(stored).hexdigest(),
            "gzip": stored[:2] == b"\x1f\x8b",
            "bytes": len(plain),
            "sha256": hashlib.sha256(plain).hexdigest(),
        }
    )
    print(f"{len(plain):>9}  {rel}  ({'gzip ' if stored[:2] == b'\\x1f\\x8b' else ''}inode {cid[:12]}…)")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cid")
    ap.add_argument("outdir")
    ap.add_argument("--chain", default="eip155:1", choices=sorted(CONTRACTS))
    ap.add_argument("--block", type=int, help="read at this block (default: the current head, recorded)")
    a = ap.parse_args()
    cid = a.cid.lower().removeprefix("onchfs://").split("/")[0]
    if len(cid) != 64 or any(c not in "0123456789abcdef" for c in cid):
        sys.exit("cid must be 64 hex characters")
    chain = Chain(a.chain, a.block)
    manifest = []
    walk(chain, cid, a.outdir, "", manifest)
    rec = {
        "onchfs": f"onchfs://{cid}",
        "chain": a.chain,
        "contract": chain.contract,
        "block": chain.block,
        "files": manifest,
    }
    with open(os.path.join(a.outdir, "onchfs-read.json"), "w") as f:
        json.dump(rec, f, indent=2)
    print(f"block {chain.block}, {len(manifest)} files, record in {a.outdir}/onchfs-read.json")


if __name__ == "__main__":
    main()
