#!/usr/bin/env bash
# Copies one static binary out of a pinned container image onto PATH.
#
#   scripts/ci/install_from_image.sh <image@sha256:...> <path-in-image> <name>
#
# CI used to curl these tools from GitHub release assets. On 2026-11-08 those
# downloads returned 504 for hours (GitHub's status page stayed green) and
# every check that needed one failed. The tools' own images come from
# registries (ghcr.io, Docker Hub) with a different serving path, are pinned
# by digest, and `docker create` retries layer pulls itself. The binary is
# the same release build, so the checks behave exactly as before.
set -euo pipefail

image=${1:?image}
path=${2:?path in image}
name=${3:?binary name}
dest=${INSTALL_DIR:-/usr/local/bin}

case "$image" in
  *@sha256:*) ;;
  *) echo "::error::$image is not pinned by digest" >&2; exit 1 ;;
esac

for attempt in 1 2 3; do
  if container=$(docker create "$image"); then
    break
  fi
  [ "$attempt" -lt 3 ] || { echo "::error::could not pull $image" >&2; exit 1; }
  echo "::warning::pulling $image failed (attempt $attempt of 3), retrying"
  sleep $((attempt * 10))
done
trap 'docker rm "$container" >/dev/null || true' EXIT

staging=$(mktemp -d)
docker cp "$container:$path" "$staging/$name"
if [ -w "$dest" ]; then install -m 0755 "$staging/$name" "$dest/$name"; else sudo install -m 0755 "$staging/$name" "$dest/$name"; fi
rm -rf "$staging"
# Prove it runs on this machine.
case "$name" in
  kubeconform) "$dest/$name" -v ;;
  *) "$dest/$name" --version ;;
esac
