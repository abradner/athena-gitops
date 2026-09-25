#! /bin/bash
set -e
source ../athena.zsh

# An athena.zsh older than the scratch workers has no NVME_WORKER_IP, and a
# loop over an unset array just runs zero times: the scratch nodes would be
# skipped silently. Require it to be declared; NVME_WORKER_IP=() means "none".
if ! declare -p NVME_WORKER_IP >/dev/null 2>&1; then
  echo "NVME_WORKER_IP is not set in athena.zsh (see athena.template.zsh; use NVME_WORKER_IP=() for none)" >&2
  exit 1
fi

for ip in "${WORKER_IP[@]}"; do
  echo "=== Applying configuration to node $ip ==="
  talosctl apply-config --insecure \
    --nodes $ip \
    --file worker.yaml
  echo "Configuration applied to $ip"
  echo ""
done

# Scratch workers: same base config plus a scheduling patch (label + soft
# taint). See worker-nvme.patch.yaml for why it is a patch.
#
# Merged with yq, NOT `talosctl apply-config --config-patch`. A 1.13
# talosctl's patcher (1.13.10 is what bootstrap/mise.toml pins) re-serializes
# the whole config and drops empty lists. The ResolverConfig's `domains: []`,
# the DHCP search-domain override (AGENTS.md Gotchas #5), then silently
# vanishes on exactly the runner nodes that need it. The 1.14.1 client keeps
# it, but nothing here enforces the client version. yq merges structurally,
# and the check below makes the result explicit either way.
if [ "${#NVME_WORKER_IP[@]}" -gt 0 ]; then
  command -v yq >/dev/null || { echo "yq is required for scratch workers (repo mise.toml pins it)" >&2; exit 1; }
  # Holds the full cluster PKI: owner-only, and removed on exit.
  nvme_config="$(mktemp)"
  chmod 600 "$nvme_config"
  trap 'rm -f "$nvme_config"' EXIT
  yq '(select(.machine) | .machine) *= load("worker-nvme.patch.yaml").machine' worker.yaml > "$nvme_config"
  # Fail here, not on a node, if the merge ever stops carrying the override.
  domains="$(yq -o=json -I=0 'select(.kind == "ResolverConfig") | .searchDomains.domains' "$nvme_config")"
  if [ "$domains" != "[]" ]; then
    echo "merged scratch config lost ResolverConfig searchDomains.domains: [] (got: '${domains}')" >&2
    exit 1
  fi
fi

for ip in "${NVME_WORKER_IP[@]}"; do
  echo "=== Applying configuration to scratch node $ip ==="
  talosctl apply-config --insecure \
    --nodes $ip \
    --file "$nvme_config"
  echo "Configuration applied to $ip"
  echo ""
done
