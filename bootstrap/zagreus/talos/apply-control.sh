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

for ip in "${CONTROL_PLANE_IP[@]}"; do
  echo "=== Applying control-plane configuration to $ip ==="
  talosctl apply-config --insecure --nodes "$ip" --file controlplane.yaml
  echo ""
done
