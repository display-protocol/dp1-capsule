#!/usr/bin/env bash
# Cross-machine check without bubblewrap: run verify inside the engine image as a Docker container.
# Docker provides the isolation here (--network none, read-only root); the harness runs directly.
# Works on a Mac with Docker Desktop (Apple Silicon runs the x86-64 image under emulation: the 2036 path).
# Mac prerequisites: Docker Desktop running; `brew install zstd`; the system python3.
#
#   ./verify-in-docker.sh engine-<sha>.tar.zst work.dp1c [work.dp1c.sig.json] [more verify args, e.g. --require-signer did:key:...]
set -euo pipefail
IMG="${1:?engine tar.zst}"; CAP="${2:?capsule}"; SIG="${3:-}"; EXTRA=("${@:4}")
HERE="$(cd "$(dirname "$0")" && pwd)"
SHA="$(shasum -a 256 "$IMG" 2>/dev/null | cut -d' ' -f1 || sha256sum "$IMG" | cut -d' ' -f1)"
PINNED="$(zstd -d -c "$CAP" | tar -xO meta/engine/chromium.json | python3 -c 'import json,sys; print(json.load(sys.stdin)["imageRef"]["sha256"])')"
echo "engine file sha256: $SHA"; echo "capsule pins:       $PINNED"
[ "$SHA" = "$PINNED" ] || { echo "engine image does not match the capsule's pin"; exit 1; }
TAG="dp1c-engine:$SHA"
if docker image inspect "$TAG" >/dev/null 2>&1; then
  LABEL="$(docker image inspect --format '{{ index .Config.Labels "dp1c.engine" }}' "$TAG")"
  [ "$LABEL" = "$SHA" ] || { echo "local image $TAG is not labelled as this engine (label: $LABEL); remove it and rerun"; exit 1; }
else
  echo ">> importing engine (one time)"; zstd -d -c "$IMG" | docker import --platform linux/amd64 --change "LABEL dp1c.engine=$SHA" - "$TAG" >/dev/null
fi
# the job directory must be somewhere the VM-backed Docker on a Mac actually mounts (Colima and Docker Desktop share $HOME,
# not the private per-user temp directory mktemp picks there)
WORK="$(mktemp -d "$HOME/dp1c-job.XXXXXX")"; trap 'rm -rf "$WORK"' EXIT
cp "$CAP" "$WORK/capsule.dp1c"; [ -n "$SIG" ] && cp "$SIG" "$WORK/capsule.dp1c.sig.json"
cp "$HERE/dp1c.py" "$HERE/harness.py" "$WORK/"
echo ">> verifying inside the container, network none"
docker run --rm --platform linux/amd64 --network none --read-only --tmpfs /tmp --tmpfs /out \
  -v "$WORK":/job:ro -e DP1C_CONTAINER_ENGINE_SHA="$SHA" -w /job \
  "$TAG" /usr/bin/python /job/dp1c.py verify /job/capsule.dp1c --container ${EXTRA[@]+"${EXTRA[@]}"}
