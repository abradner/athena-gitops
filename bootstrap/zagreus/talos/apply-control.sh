#! /bin/bash
# Apply the hydrated control-plane config to zagreus's control-plane node(s)
# in maintenance mode. See bootstrap/zagreus/README.md for the order of steps.
# No -u: macOS's bash 3.2 treats an empty array as unbound, and an empty node
# class (e.g. ARM_BUILDER_IP=()) is legitimate.
set -eo pipefail
cd "$(dirname "$0")"
source ../zagreus.zsh

# A file still holding a {{ placeholder }} was not hydrated (or the 1Password
# note lacks a key); applying it would hand Talos a literal template.
if grep -q '{{' controlplane.yaml; then
  echo "controlplane.yaml still contains {{ placeholders }}: re-run render-talos for zagreus" >&2
  exit 1
fi

# Talos 1.14 config needs a 1.14 client: a 1.13 talosctl's patcher turns the
# ResolverConfig's `domains: []` into null, and the node then takes DHCP's
# search domains (athena-gitops PR #146 measured it). Refuse an old
# client, and check the merged config itself before anything is applied.
command -v talosctl >/dev/null || { echo "talosctl not found (use bootstrap/zagreus/mise.toml)" >&2; exit 1; }
client_minor=$(talosctl version --client --short 2>/dev/null | sed -nE 's/.*v1\.([0-9]+)\..*/\1/p' | head -1)
if [ -z "$client_minor" ] || [ "$client_minor" -lt 14 ]; then
  echo "talosctl client is $(talosctl version --client --short 2>/dev/null | tail -1); zagreus needs 1.14+ (bootstrap/zagreus/mise.toml)" >&2
  exit 1
fi
command -v yq >/dev/null || { echo "yq is required to check the merged config" >&2; exit 1; }
domains_are_empty() { # <merged config file>
  [ "$(yq 'select(.kind == "ResolverConfig") | .searchDomains.domains | tag + ":" + (length | tostring)' "$1")" = "!!seq:0" ]
}

# Without -u a missing or misspelled CONTROL_PLANE_IP would loop zero times
# and exit 0, reading as success.
if ! declare -p CONTROL_PLANE_IP >/dev/null 2>&1 || [ "${#CONTROL_PLANE_IP[@]}" -eq 0 ]; then
  echo "CONTROL_PLANE_IP is missing or empty in zagreus.zsh" >&2
  exit 1
fi

if ! domains_are_empty controlplane.yaml; then
  echo "controlplane.yaml: ResolverConfig searchDomains.domains is not an empty list" >&2
  exit 1
fi

for ip in "${CONTROL_PLANE_IP[@]}"; do
  echo "=== Applying control-plane configuration to $ip ==="
  talosctl apply-config --insecure --nodes "$ip" --file controlplane.yaml
  echo ""
done
