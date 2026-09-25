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
for ip in "${NVME_WORKER_IP[@]}"; do
  echo "=== Applying configuration to scratch node $ip ==="
  talosctl apply-config --insecure \
    --nodes $ip \
    --file worker.yaml \
    --config-patch @worker-nvme.patch.yaml
  echo "Configuration applied to $ip"
  echo ""
done
