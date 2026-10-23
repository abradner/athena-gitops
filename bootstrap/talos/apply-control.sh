#! /bin/bash
set -ex
source ../athena.zsh

# The control planes' shared API address: a Talos Layer2 VIP on the CP VMs'
# NIC. It comes from athena.zsh so that no address is committed here, and it
# is what the LAN's DNS answers for the control-plane endpoint name, so
# kubectl survives losing any one control plane, or the WAN. Declared but
# empty means no VIP. See "Control plane VIP" in bootstrap/README.md.
if ! declare -p CONTROL_PLANE_VIP >/dev/null 2>&1; then
  echo "CONTROL_PLANE_VIP is not set in athena.zsh (see athena.template.zsh; use \"\" for none)" >&2
  exit 1
fi
vip_patch=()
if [ -n "$CONTROL_PLANE_VIP" ]; then
  vip_file=$(mktemp)
  trap 'rm -f -- "$vip_file"' EXIT
  printf 'apiVersion: v1alpha1\nkind: Layer2VIPConfig\nname: %s\nlink: enp0s11\n' "$CONTROL_PLANE_VIP" > "$vip_file"
  vip_patch=(--config-patch "@$vip_file")
fi

for ip in "${CONTROL_PLANE_IP[@]}"; do
  echo "=== Applying configuration to node $ip ==="
  # ${arr[@]+...}: an empty array is "unbound" under macOS's bash 3.2.
  talosctl apply-config --insecure \
    --nodes $ip \
    --file controlplane.yaml \
    ${vip_patch[@]+"${vip_patch[@]}"}
  echo "Configuration applied to $ip"
  echo ""
done
