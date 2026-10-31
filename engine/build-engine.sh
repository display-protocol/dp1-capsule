#!/usr/bin/env bash
# Build a content-addressed DP-1 render engine from the Arch Linux package archive.
# No root, no daemon: runs pacman inside a user namespace (unshare -r).
#
#   engine/build-engine.sh 2026/10/20 out/
#
# Output: out/engine-<sha256>.tar.zst and out/engine-<sha256>.json
set -euo pipefail
DATE="${1:?archive date YYYY/MM/DD}"; OUT="${2:?output dir}"; CHROMIUM_URL="${3:-}"   # optional: exact chromium pkg URL to install over the snapshot
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/dp1-engine.XXXXXX")"
ROOT="$WORK/rootfs"; mkdir -p "$ROOT" "$OUT"
PKGS=(bash coreutils glibc chromium ttf-dejavu ttf-liberation noto-fonts-cjk python python-pillow python-zstandard python-cryptography)

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

export CHROMIUM_URL
echo ">> installing ${PKGS[*]} from archive $DATE ${CHROMIUM_URL:+(chromium from $CHROMIUM_URL)}"
unshare -r bash -c '
  set -euo pipefail
  ROOT="$1"; CONF="$2"; shift 2
  mkdir -p "$ROOT/var/lib/pacman" "$ROOT/var/cache/pacman/pkg" "$ROOT/.nohooks"
  # hooks are skipped (they assume a booted system); the two that matter are run by hand below
  pacman -r "$ROOT" --config "$CONF" --dbpath "$ROOT/var/lib/pacman" \
         --cachedir "$ROOT/var/cache/pacman/pkg" --hookdir "$ROOT/.nohooks" \
         --noconfirm --noprogressbar -Sy "$@"
  if [ -n "$CHROMIUM_URL" ]; then
    pacman -r "$ROOT" --config "$CONF" --dbpath "$ROOT/var/lib/pacman" \
           --cachedir "$ROOT/var/cache/pacman/pkg" --hookdir "$ROOT/.nohooks" \
           --noconfirm --noprogressbar -U "$CHROMIUM_URL"
  fi
  rmdir "$ROOT/.nohooks"
  chroot "$ROOT" /usr/bin/ldconfig 2>/dev/null || true
  chroot "$ROOT" /usr/bin/fc-cache -f >/dev/null 2>&1 || true
  chroot "$ROOT" /usr/bin/gdk-pixbuf-query-loaders --update-cache >/dev/null 2>&1 || true
  chroot "$ROOT" /usr/bin/glib-compile-schemas /usr/share/glib-2.0/schemas >/dev/null 2>&1 || true
' _ "$ROOT" "$WORK/pacman.conf" "${PKGS[@]}"


echo ">> recording packages"
PKGLIST="$(unshare -r pacman -r "$ROOT" --dbpath "$ROOT/var/lib/pacman" -Q | sort)"

echo ">> mount points for the sandbox (ro-bind root needs them to exist)"
mkdir -p "$ROOT/dev" "$ROOT/proc" "$ROOT/tmp" "$ROOT/work" "$ROOT/out"

echo ">> recording package file hashes (the rebuild recipe: exact bytes, not just versions)"
PKGSHA="$WORK/pkgsha.txt"; : > "$PKGSHA"
for f in "$ROOT"/var/cache/pacman/pkg/*.pkg.tar.zst; do [ -f "$f" ] && echo "$(basename "$f") $(sha256sum "$f" | cut -d' ' -f1)" >> "$PKGSHA"; done
mkdir -p "$ROOT/opt/dp1c"
cat > "$ROOT/opt/dp1c/ENGINE" <<E
archive=$DATE
chromium=$(echo "$PKGLIST" | awk '$1=="chromium"{print $2}')
E

echo ">> normalizing install-time state (machine id, install dates, logs)"
unshare -r bash -c 'chmod u+w "$1/etc/machine-id" 2>/dev/null; : > "$1/etc/machine-id"; for d in "$1"/var/lib/pacman/local/*/desc; do sed -i "/^%INSTALLDATE%$/,/^$/d" "$d"; done' _ "$ROOT"
rm -f "$ROOT/var/lib/pacman/local/ALPM_DB_VERSION.lock" "$ROOT/var/lib/pacman/db.lck" 2>/dev/null || true
find "$ROOT/var/log" -type f -delete 2>/dev/null || true
TOOLS="$(printf 'pacman %s; tar %s; zstd %s; python %s' "$(pacman -Q pacman | cut -d" " -f2)" "$(tar --version | head -1 | awk "{print \$NF}")" "$(zstd --version | grep -o 'v[0-9.]*' | head -1)" "$(python3 --version | cut -d" " -f2)")"

echo ">> pruning"
rm -rf "$ROOT/var/cache/pacman/pkg"/* "$ROOT/usr/share/doc" "$ROOT/usr/share/man" "$ROOT/usr/share/info" \
       "$ROOT/usr/share/locale" "$ROOT/var/log"/* "$ROOT/usr/lib/python3*/test" 2>/dev/null || true

echo ">> packing (deterministic tar: sorted, mtime 0, numeric owner)"
TAR="$WORK/engine.tar"
unshare -r tar --sort=name --mtime='1970-01-01 00:00:00Z' --numeric-owner --owner=0 --group=0 \
    --pax-option=exthdr.name=%d/PaxHeaders/%f,delete=atime,delete=ctime \
    -C "$ROOT" -cf "$TAR" .
zstd -q -19 --single-thread --no-check -f "$TAR" -o "$WORK/engine.tar.zst"   # single thread: the MT encoder is not a stable function of its input
HASH="$(sha256sum "$WORK/engine.tar.zst" | cut -d' ' -f1)"
mv "$WORK/engine.tar.zst" "$OUT/engine-$HASH.tar.zst"

printf '%s\n' "$PKGLIST" > "$WORK/pkglist.txt"
python3 - "$OUT/engine-$HASH.json" "$HASH" "$DATE" "$WORK/pkglist.txt" "${CHROMIUM_URL:-from snapshot}" "$TOOLS" "$PKGSHA" <<'PY'
import json, sys
out, h, date, pkfile = sys.argv[1:5]
pk = {}
for line in open(pkfile).read().splitlines():
    n, v = line.split()
    pk[n] = v
files = {}
for line in open(sys.argv[7]).read().splitlines():
    fname, fhash = line.split()
    files[fname] = fhash
json.dump({
  "engine": "chromium", "version": pk["chromium"].rsplit("-", 1)[0], "platform": "linux-x86_64",
  "imageRef": {"sha256": h, "format": "tar+zstd rootfs", "source": "https://archive.archlinux.org/repos/%s/" % date,
               "chromiumPackage": sys.argv[5],
               "buildTools": sys.argv[6],
               "hooks": "alpm hooks from the rootfs run during install (22 on 2026-10-30, some fail harmlessly); ldconfig, fc-cache, pixbuf loaders and glib schemas are rerun by hand",
               "packages": pk, "packageFiles": files,
               "run": "see dp1c.py render(): bwrap --ro-bind <rootfs> / --dev /dev --proc /proc --tmpfs /tmp --ro-bind <assets> /work --bind <out> /out --ro-bind <harness dir> /harness --unshare-all --uid 0 --gid 0 --cap-add CAP_NET_ADMIN --chdir / --clearenv [env] /bin/bash -c 'mkdir -p /tmp/xdg /tmp/xdgc /tmp/run /tmp/profile && exec /usr/bin/python /harness/harness.py' with the render config on stdin; harness.py brings lo up, drops caps via setpriv, serves /work on 127.0.0.1 and drives chromium over the DevTools pipe"},
}, open(out, "w"), indent=2)
PY
du -sh "$ROOT" | cut -f1 | xargs echo ">> rootfs size:"
ls -la "$OUT/engine-$HASH.tar.zst" | awk '{print ">> image:", $5, "bytes"}'
echo ">> engine sha256 $HASH"
unshare -r rm -rf "$WORK"   # rootfs has 0555 dirs and root-owned files
