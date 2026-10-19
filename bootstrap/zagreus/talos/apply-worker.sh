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

# Talos 1.14 config needs a 1.14 client: a 1.13 talosctl's patcher turns the
# ResolverConfig's `domains: []` into null, and the node then takes DHCP's
# search domains (athena-gitops Gotchas #5; #146 measured it). Refuse an old
# client, and check the merged config itself before anything is applied.
client_minor=$(talosctl version --client --short 2>/dev/null | sed -nE 's/.*v1\.([0-9]+)\..*/\1/p' | head -1)
if [ -z "$client_minor" ] || [ "$client_minor" -lt 14 ]; then
  echo "talosctl client is $(talosctl version --client --short 2>/dev/null | tail -1); zagreus needs 1.14+ (bootstrap/zagreus/mise.toml)" >&2
  exit 1
fi
command -v yq >/dev/null || { echo "yq is required to check the merged config" >&2; exit 1; }
domains_are_empty() { # <merged config file>
  [ "$(yq 'select(.kind == "ResolverConfig") | .searchDomains.domains | tag + ":" + (length | tostring)' "$1")" = "!!seq:0" ]
}

# Every class array must be declared, even empty: a loop over an unset array
# runs zero times, and a node class would be skipped without a word.
for var in ARM_RUNNER_IP ARM_BUILDER_IP AMD64_RUNNER_IP; do
  if ! declare -p "$var" >/dev/null 2>&1; then
    echo "$var is not set in zagreus.zsh (see zagreus.template.zsh; use $var=() for none)" >&2
    exit 1
  fi
done

# Merged configs hold the cluster PKI: keep them in one private directory and
# remove it however the script ends, including a failed apply under set -e.
merged_dir=$(mktemp -d)
trap 'rm -rf -- "$merged_dir"' EXIT

apply() { # <ip> <patch> <class> [extra patch]
  local extra=()
  if [ -n "${4:-}" ]; then
    [ -f "$4" ] || { echo "extra patch '$4' for $1 not found" >&2; exit 1; }
    extra=(--patch "@$4")
  fi
  # Merge locally first, check the result, then apply exactly that file.
  local merged="$merged_dir/$1.yaml"
  talosctl machineconfig patch worker.yaml --patch "@$2" ${extra[@]+"${extra[@]}"} -o "$merged"  # empty-array safe on macOS bash 3.2
  if ! domains_are_empty "$merged"; then
    echo "$1: merged config lost ResolverConfig searchDomains.domains: [] (patcher too old?)" >&2
    exit 1
  fi
  echo "=== Applying $3 configuration to $1${4:+ (+ $4)} ==="
  talosctl apply-config --insecure --nodes "$1" --file "$merged"
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
