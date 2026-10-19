#! /bin/bash
# Apply the hydrated worker config, plus each node class's patch, to zagreus
# workers in maintenance mode. See bootstrap/zagreus/README.md.
# No -u: macOS's bash 3.2 treats an empty array as unbound, and an empty node
# class (e.g. ARM_BUILDER_IP=()) is legitimate.
set -eo pipefail
cd "$(dirname "$0")"
source ../zagreus.zsh

if grep -q '{{' worker.yaml; then
  echo "worker.yaml still contains {{ placeholders }}: re-run render-talos for zagreus" >&2
  exit 1
fi

# Every class array must be declared, even empty: a loop over an unset array
# runs zero times, and a node class would be skipped without a word.
for var in ARM_RUNNER_IP ARM_BUILDER_IP AMD64_RUNNER_IP; do
  if ! declare -p "$var" >/dev/null 2>&1; then
    echo "$var is not set in zagreus.zsh (see zagreus.template.zsh; use $var=() for none)" >&2
    exit 1
  fi
done

apply() { # <ip> <patch> <class> [extra patch]
  local extra=()
  if [ -n "${4:-}" ]; then
    [ -f "$4" ] || { echo "extra patch '$4' for $1 not found" >&2; exit 1; }
    extra=(--config-patch "@$4")
  fi
  echo "=== Applying $3 configuration to $1${4:+ (+ $4)} ==="
  talosctl apply-config --insecure --nodes "$1" --file worker.yaml --config-patch "@$2" ${extra[@]+"${extra[@]}"}  # empty-array safe on macOS bash 3.2
  echo ""
}

for ip in "${ARM_RUNNER_IP[@]}";   do apply "$ip" worker-arm-runner.patch.yaml  arm-runner;   done
for ip in "${ARM_BUILDER_IP[@]}";  do apply "$ip" worker-arm-builder.patch.yaml arm-builder;  done
# An amd64 entry is "<ip>" or "<ip>:<path to an extra patch>", for a box whose
# disks need more than worker-amd64.patch.yaml (the 800 G3 with two SSDs and
# an HDD). Per-node patches name real disks, so they live in the private
# infrastructure repo, not here.
for entry in "${AMD64_RUNNER_IP[@]}"; do
  ip="${entry%%:*}"; extra=""
  [ "$entry" != "$ip" ] && extra="${entry#*:}"
  apply "$ip" worker-amd64.patch.yaml amd64-runner "$extra"
done
