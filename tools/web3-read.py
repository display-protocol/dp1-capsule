#!/usr/bin/env python3
"""Read a work from a contract call named by a web3:// URL (ERC-4804, with ERC-6860's corrections) and write it as a file.

  tools/web3-read.py 'web3://0x4f32740cf412d647a622ba27c36fa4b76f71601f:1/artpiece' workdir/ [--block N] [--name index.html]

A web3:// URL names a contract, an optional chain id (default 1), a method and its arguments, so a work that lives
behind a view function (Han's artpiece(), Art Blocks' getTokenHtml(core, tokenId)) has an address any player can
resolve with one eth_call and no knowledge of the artist. Auto mode only (resolveMode() is honoured when the
contract has it: "manual" sends the path and query as raw calldata); a literal 0x address, no name service.
Argument types are inferred as the standard says (uint256 for integers, bytes32 / address / bytes for 0x hex by
length, string otherwise) or given explicitly as type!value. Without a returns attribute the result is decoded as
ABI bytes, which is also how a string comes back; with returns=(...) the decoded values are written as JSON.

There is no hash to verify a call's result against: the chain's consensus is the guarantee, so the node matters
(use your own, or compare two). The read is pinned to one block and recorded in <workdir>/web3-read.json with the
calldata and the sha256 of what came back, so a later reader can repeat the exact call."""

import argparse, hashlib, importlib.util, json, os, re, sys, urllib.parse, urllib.request

_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("onchfs_read", os.path.join(_here, "onchfs-read.py"))
_onchfs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_onchfs)
keccak256 = _onchfs.keccak256

RPCS = {
    1: [
        "https://ethereum-rpc.publicnode.com",
        "https://eth-mainnet.public.blastapi.io",
        "https://1rpc.io/eth",
        "https://rpc.mevblocker.io",
    ],
    8453: ["https://base-rpc.publicnode.com", "https://mainnet.base.org"],
    10: ["https://optimism-rpc.publicnode.com"],
    42161: ["https://arbitrum-one-rpc.publicnode.com"],
    11155111: ["https://ethereum-sepolia-rpc.publicnode.com"],
}
MIME_BY_EXT = {
    ".html": "text/html",
    ".htm": "text/html",
    ".svg": "image/svg+xml",
    ".json": "application/json",
    ".txt": "text/plain",
    ".js": "text/javascript",
    ".css": "text/css",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".gif": "image/gif",
    ".mp4": "video/mp4",
    ".webm": "video/webm",
}


def parse_web3_url(url):
    """-> dict(contract, chain_id, method, args=[(type, value)], returns=[types] or None, ext)"""
    m = re.match(r"^(?:web3|w3)://(?:[^@/]*@)?([^/:?#]+)(?::(\d+))?(/[^?#]*)?(?:\?([^#]*))?(?:#.*)?$", url.strip())
    if not m:
        sys.exit(f"not a web3:// URL: {url}")
    contract, chain, path, query = m.group(1), m.group(2), m.group(3) or "/", m.group(4) or ""
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", contract):
        sys.exit("the contract must be a literal 0x address (name service lookup is not implemented)")
    segs = [urllib.parse.unquote(s) for s in path.split("/")[1:] if s != ""]
    method = segs[0] if segs else None
    args = []
    for raw in segs[1:]:
        if "!" in raw:
            t, v = raw.split("!", 1)
        else:
            if re.fullmatch(r"\d+", raw):
                t, v = "uint256", raw
            elif re.fullmatch(r"0x[0-9a-fA-F]{64}", raw):
                t, v = "bytes32", raw
            elif re.fullmatch(r"0x[0-9a-fA-F]{40}", raw):
                t, v = "address", raw
            elif re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", raw):
                t, v = "bytes", raw
            else:
                t, v = "string", raw
        args.append((t, v))
    returns = None
    for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True):
        if k in ("returns", "returnTypes"):
            inner = v.strip()
            if not (inner.startswith("(") and inner.endswith(")")):
                sys.exit("returns must look like (type,type)")
            returns = [t.strip() for t in inner[1:-1].split(",") if t.strip()]
    ext = os.path.splitext(segs[-1])[1].lower() if segs else ""
    if ext and method and segs[-1] == method:
        ext = ""  # a method name is not a file name
    return {
        "contract": contract.lower(),
        "chain_id": int(chain) if chain else 1,
        "method": method,
        "args": args,
        "returns": returns,
        "ext": ext,
        "path": path,
        "query": query,
    }


def enc_arg(t, v):
    """Static ABI encoding for the argument types auto mode produces; dynamic ones (string, bytes) go to the tail."""
    if t.startswith("uint") or t.startswith("int"):
        n = int(v, 0)
        return (n % (1 << 256)).to_bytes(32, "big"), None
    if t == "address":
        return bytes.fromhex(v[2:]).rjust(32, b"\0"), None
    if t == "bytes32" or re.fullmatch(r"bytes([1-9]|[12]\d|3[0-2])", t):
        return bytes.fromhex(v[2:]).ljust(32, b"\0"), None
    if t == "bool":
        return (1 if v in ("true", "1") else 0).to_bytes(32, "big"), None
    if t == "bytes":
        b = bytes.fromhex(v[2:])
    elif t == "string":
        b = v.encode()
    else:
        sys.exit(f"argument type {t} is not supported")
    tail = len(b).to_bytes(32, "big") + b + b"\0" * ((-len(b)) % 32)
    return None, tail


def abi_encode(args):
    heads, tails = [], []
    for t, v in args:
        h, tl = enc_arg(t, v)
        heads.append(h)
        tails.append(tl)
    out = b""
    dyn_base = 32 * len(args)
    tail_bytes = b""
    for h, tl in zip(heads, tails):
        if h is not None:
            out += h
        else:
            out += (dyn_base + len(tail_bytes)).to_bytes(32, "big")
            tail_bytes += tl
    return out + tail_bytes


def word(b, i):
    return int.from_bytes(b[32 * i : 32 * i + 32], "big")


def dec_dyn(b, off):
    ln = word(b, off // 32)
    return b[off + 32 : off + 32 + ln]


def decode_returns(b, types):
    out = []
    for i, t in enumerate(types):
        if t in ("string", "bytes"):
            v = dec_dyn(b, word(b, i))
            out.append(v.decode(errors="replace") if t == "string" else "0x" + v.hex())
        elif t.startswith("uint") or t.startswith("int") or t == "bool":
            out.append(hex(word(b, i)))
        elif t == "address":
            out.append("0x" + b[32 * i + 12 : 32 * i + 32].hex())
        else:
            out.append("0x" + b[32 * i : 32 * i + 32].hex())
    return out


class Chain:
    def __init__(self, chain_id, block=None):
        if chain_id not in RPCS:
            sys.exit(f"no public RPC known for chain id {chain_id}; add one to RPCS")
        self.rpcs = RPCS[chain_id]
        self.block = block if block is not None else int(self.rpc("eth_blockNumber", []), 16)

    def rpc(self, method, params):
        err = None
        for rpc in self.rpcs:
            try:
                req = urllib.request.Request(
                    rpc,
                    data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(),
                    headers={"Content-Type": "application/json", "User-Agent": "dp1c-web3-read"},
                )
                r = json.load(urllib.request.urlopen(req, timeout=60))
                if "result" in r:
                    return r["result"]
                err = r.get("error")
                if isinstance(err, dict) and err.get("code") == 3:
                    return None  # execution reverted
            except Exception as e:  # noqa: BLE001
                err = str(e)
        sys.exit(f"every RPC failed: {err}")

    def call(self, to, data):
        r = self.rpc("eth_call", [{"to": to, "data": "0x" + data.hex()}, hex(self.block)])
        return None if r is None else bytes.fromhex(r[2:])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("outdir")
    ap.add_argument("--block", type=int, help="read at this block (default: the current head, recorded)")
    ap.add_argument("--name", help="file name to write (default: index.html for an HTML result, else by MIME)")
    ap.add_argument("--mime", help="MIME type when the URL does not say (default text/html: this tool reads works)")
    a = ap.parse_args()
    u = parse_web3_url(a.url)
    chain = Chain(u["chain_id"], a.block)
    mode = "auto"
    rm = chain.call(u["contract"], keccak256(b"resolveMode()")[:4])
    if rm and len(rm) >= 32:
        word0 = rm[:32].rstrip(b"\0")
        if word0 == b"manual":
            mode = "manual"
        elif word0 not in (b"", b"auto"):
            sys.exit(f"unsupported resolve mode {word0!r}")
    if mode == "manual":
        calldata = (u["path"] + ("?" + u["query"] if u["query"] else "")).encode()
    elif u["method"] is None:
        calldata = b""
    else:
        sig = f"{u['method']}({','.join(t for t, _ in u['args'])})"
        calldata = keccak256(sig.encode())[:4] + abi_encode(u["args"])
    out = chain.call(u["contract"], calldata)
    if out is None:
        sys.exit("the call reverted: no work at that address")
    mime = a.mime or MIME_BY_EXT.get(u["ext"]) or "text/html"
    if u["returns"] is None:
        body = dec_dyn(out, word(out, 0)) if len(out) >= 64 else out  # ABI bytes (a string encodes the same way)
        name = a.name or ("index.html" if mime == "text/html" else "result" + (u["ext"] or ""))
    elif u["returns"] == []:
        body = json.dumps(["0x" + out.hex()]).encode()
        mime, name = "application/json", a.name or "result.json"
    else:
        body = json.dumps(decode_returns(out, u["returns"])).encode()
        mime, name = "application/json", a.name or "result.json"
    os.makedirs(a.outdir, exist_ok=True)
    with open(os.path.join(a.outdir, name), "wb") as f:
        f.write(body)
    rec = {
        "web3": a.url.strip(),
        "chain": f"eip155:{u['chain_id']}",
        "contract": u["contract"],
        "resolveMode": mode,
        "calldata": "0x" + calldata.hex(),
        "block": chain.block,
        "returnedBytes": len(out),
        "returnedSha256": hashlib.sha256(out).hexdigest(),
        "file": name,
        "mime": mime,
        "bytes": len(body),
        "sha256": hashlib.sha256(body).hexdigest(),
        "verified": "none: a call has no content address; trust is the node's consensus, pinned to this block",
    }
    with open(os.path.join(a.outdir, "web3-read.json"), "w") as f:
        json.dump(rec, f, indent=2)
    print(f"{len(body):>9}  {name}  ({mime}; {mode} mode; block {chain.block}; sha256 {rec['sha256'][:12]}…)")


if __name__ == "__main__":
    main()
