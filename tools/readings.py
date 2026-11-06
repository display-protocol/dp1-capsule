#!/usr/bin/env python3
"""The readings behind DEFAULT_TOLERANCE, recomputed from the example capsules' reference frames.

  python3 tools/readings.py            # prints one row per example: row/column dHash distance per transformation

Each transformation is spelled out here so the numbers in dp1c.py and SPEC-PROPOSAL.md §5 can be reproduced exactly.
Rows: dp1c.dhash (9x8, left > right). Columns: dp1c.dhash_v (8x9, upper > lower). Pillow's Lanczos resize in both."""

import glob, io, os, subprocess, sys, tarfile

from PIL import Image, ImageEnhance, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import dp1c


def reference_png(capsule):
    raw = subprocess.run(["zstd", "-dc", capsule], capture_output=True, check=True).stdout
    t = tarfile.open(fileobj=io.BytesIO(raw))
    return Image.open(io.BytesIO(t.extractfile("meta/render/reference.png").read())).convert("RGB")


def transformations(im):
    W, H = im.size
    jpg = io.BytesIO()
    im.save(jpg, "JPEG", quality=60)
    shift = Image.new("RGB", (W, H), (0, 0, 0))
    shift.paste(im, (2, 1))
    halves_ud = Image.new("RGB", (W, H))
    halves_ud.paste(im.crop((0, H // 2, W, H)), (0, 0))
    halves_ud.paste(im.crop((0, 0, W, H // 2)), (0, H // 2))
    halves_lr = Image.new("RGB", (W, H))
    halves_lr.paste(im.crop((W // 2, 0, W, H)), (0, 0))
    halves_lr.paste(im.crop((0, 0, W // 2, H)), (W // 2, 0))
    return [
        ("jpeg q60", Image.open(jpg).convert("RGB")),
        ("blur 1.5px", im.filter(ImageFilter.GaussianBlur(1.5))),
        ("gamma 0.8 (v -> 255*(v/255)**0.8, truncated)", im.point(lambda v: int(255 * ((v / 255) ** 0.8)))),
        ("brightness x1.1", ImageEnhance.Brightness(im).enhance(1.1)),
        ("saturation x1.4", ImageEnhance.Color(im).enhance(1.4)),
        ("shift 2px right 1px down, black fill", shift),
        ("crop right 5% (keep 0..95% width), resize back", im.crop((0, 0, int(W * 0.95), H)).resize((W, H), Image.LANCZOS)),
        ("crop 5% each edge, resize back", im.crop((int(W * 0.05), int(H * 0.05), int(W * 0.95), int(H * 0.95))).resize((W, H), Image.LANCZOS)),
        ("blank black", Image.new("RGB", (W, H), (0, 0, 0))),
        ("halves swapped top/bottom", halves_ud),
        ("halves swapped left/right", halves_lr),
        ("mirrored left-right", im.transpose(Image.FLIP_LEFT_RIGHT)),
        ("mirrored top-bottom", im.transpose(Image.FLIP_TOP_BOTTOM)),
        ("rotated 180", im.transpose(Image.ROTATE_180)),
    ]


def main():
    caps = sorted(glob.glob(os.path.join(ROOT, "examples", "*.dp1c")))
    refs = {os.path.basename(c)[:-5]: reference_png(c) for c in caps}
    names = list(refs)
    ts = [n for n, _ in transformations(next(iter(refs.values())))]
    width = max(len(t) for t in ts)
    print(f"default tolerance {dp1c.DEFAULT_TOLERANCE}; cells are row/column dHash bits from the reference frame")
    print(" " * width + "  " + "  ".join(f"{n[:12]:>12}" for n in names))
    rows = {t: [] for t in ts}
    for n, im in refs.items():
        hr, hc = dp1c.dhash(im), dp1c.dhash_v(im)
        for t, x in transformations(im):
            rows[t].append(f"{dp1c.hamming(hr, dp1c.dhash(x))}/{dp1c.hamming(hc, dp1c.dhash_v(x))}")
    for t in ts:
        print(f"{t:<{width}}  " + "  ".join(f"{v:>12}" for v in rows[t]))
    print("\nanother work (row/column), each pair once:")
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = refs[names[i]], refs[names[j]]
            print(f"  {names[i]} vs {names[j]}: {dp1c.hamming(dp1c.dhash(a), dp1c.dhash(b))}/{dp1c.hamming(dp1c.dhash_v(a), dp1c.dhash_v(b))}")


if __name__ == "__main__":
    main()
