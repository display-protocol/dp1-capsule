#!/usr/bin/env bash
# Build a content-addressed DP-1 render engine from the Arch Linux package archive.
# No root, no daemon: runs pacman inside a user namespace (unshare -r).
#
#   engine/build-engine.sh 2026/10/20 out/
#
# Output: out/engine-<sha256>.tar.zst and out/engine-<sha256>.json
set -euo pipefail
DATE="${1:?archive date YYYY/MM/DD}"; OUT="${2:?output dir}"
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/dp1-engine.XXXXXX")"
ROOT="$WORK/rootfs"; mkdir -p "$ROOT" "$OUT"
PKGS=(bash coreutils glibc chromium ttf-dejavu ttf-liberation noto-fonts-cjk python python-pillow python-zstandard)

cat > "$WORK/pacman.conf" <<CONF
[options]
Architecture = x86_64
SigLevel = Never
CheckSpace
[core]
Server = https://archive.archlinux.org/repos/$DATE/\$repo/os/\$arch
[extra]
Server = https://archive.archlinux.org/repos/$DATE/\$repo/os/\$arch
CONF

echo ">> installing ${PKGS[*]} from archive $DATE"
unshare -r bash -c '
  set -euo pipefail
  ROOT="$1"; CONF="$2"; shift 2
  mkdir -p "$ROOT/var/lib/pacman" "$ROOT/var/cache/pacman/pkg"
  pacman -r "$ROOT" --config "$CONF" --dbpath "$ROOT/var/lib/pacman" \
         --cachedir "$ROOT/var/cache/pacman/pkg" --hookdir /dev/null \
         --noconfirm --noprogressbar -Sy "$@"
  # fontconfig cache so first render does not scan
  chroot "$ROOT" /usr/bin/fc-cache -f >/dev/null 2>&1 || true
' _ "$ROOT" "$WORK/pacman.conf" "${PKGS[@]}"

echo ">> recording packages"
PKGLIST="$(unshare -r pacman -r "$ROOT" --dbpath "$ROOT/var/lib/pacman" -Q | sort)"

echo ">> installing harness"
install -Dm755 "$HERE/../dp1c.py" "$ROOT/opt/dp1c/dp1c.py"
cat > "$ROOT/opt/dp1c/ENGINE" <<E
archive=$DATE
chromium=$(echo "$PKGLIST" | awk '$1=="chromium"{print $2}')
E

echo ">> pruning"
rm -rf "$ROOT/var/cache/pacman/pkg"/* "$ROOT/usr/share/doc" "$ROOT/usr/share/man" "$ROOT/usr/share/info" \
       "$ROOT/usr/share/locale" "$ROOT/var/log"/* "$ROOT/usr/lib/python3*/test" 2>/dev/null || true

echo ">> packing (deterministic tar: sorted, mtime 0, numeric owner)"
TAR="$WORK/engine.tar"
unshare -r tar --sort=name --mtime='1970-01-01 00:00:00Z' --numeric-owner --owner=0 --group=0 \
    --pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime \
    -C "$ROOT" -cf "$TAR" .
zstd -q -19 -T0 --no-check -f "$TAR" -o "$WORK/engine.tar.zst"
HASH="$(sha256sum "$WORK/engine.tar.zst" | cut -d' ' -f1)"
mv "$WORK/engine.tar.zst" "$OUT/engine-$HASH.tar.zst"

python3 - "$OUT/engine-$HASH.json" "$HASH" "$DATE" <<PY <<<"$PKGLIST"
import json, sys
out, h, date = sys.argv[1:4]
pk = {}
for line in sys.stdin.read().splitlines():
    n, v = line.split()
    pk[n] = v
json.dump({
  "engine": "chromium", "version": pk["chromium"].split("-")[0], "platform": "linux-x86_64",
  "imageRef": {"sha256": h, "format": "tar+zstd rootfs", "source": "https://archive.archlinux.org/repos/%s/" % date,
               "packages": pk, "run": "bwrap --unshare-all --ro-bind <rootfs> / ... /usr/bin/chromium"},
}, open(out, "w"), indent=2)
PY
du -sh "$ROOT" | cut -f1 | xargs echo ">> rootfs size:"
ls -la "$OUT/engine-$HASH.tar.zst" | awk '{print ">> image:", $5, "bytes"}'
echo ">> engine sha256 $HASH"
rm -rf "$WORK"
