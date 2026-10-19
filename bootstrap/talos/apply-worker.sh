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
# Same rule for the GPU workers (worker-gpu.patch.yaml).
if ! declare -p GPU_WORKER_IP >/dev/null 2>&1; then
  echo "GPU_WORKER_IP is not set in athena.zsh (see athena.template.zsh; use GPU_WORKER_IP=() for none)" >&2
  exit 1
fi
# And for the GPU workers that are VMs (worker-gpu-vm.patch.yaml).
if ! declare -p GPU_VM_WORKER_IP >/dev/null 2>&1; then
  echo "GPU_VM_WORKER_IP is not set in athena.zsh (see athena.template.zsh; use GPU_VM_WORKER_IP=() for none)" >&2
  exit 1
fi

# "<ip>:<gen>" -> sets ip and gen, or exits. Shared by both GPU lists.
split_gpu_entry() {
  ip="${1%%:*}"
  gen="${1#*:}"
  if [ "$gen" = "$1" ] || [ -z "$gen" ]; then
    echo "GPU entry '$1' has no generation; use <ip>:<gen>, where <gen> is the iGPU generation (9, 9.5, 11)" >&2
    exit 1
  fi
}

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

# GPU workers: amd64 boxes with an Intel iGPU for the spritz transcoder. Same
# base config plus worker-gpu.patch.yaml (storage layout, swap, hard taint).
# Entries are "<ip>:<gen>"; the generation becomes the spritz.dev/gpu-gen
# label. See docs/runbook-gpu-node-onboarding.md.
for entry in "${GPU_WORKER_IP[@]}"; do
  split_gpu_entry "$entry"
  echo "=== Applying configuration to GPU node $ip (gen $gen) ==="
  talosctl apply-config --insecure \
    --nodes $ip \
    --file worker.yaml \
    --config-patch @worker-gpu.patch.yaml \
    --config-patch "{\"machine\":{\"nodeLabels\":{\"spritz.dev/gpu-gen\":\"$gen\"}}}"
  echo "Configuration applied to $ip"
  echo ""
done

# GPU workers that are Proxmox VMs with the host's iGPU passed through. Same
# base config plus worker-gpu-vm.patch.yaml (disks selected by size, plain
# installer, no swap). See docs/runbook-gpu-node-onboarding.md.
for entry in "${GPU_VM_WORKER_IP[@]}"; do
  split_gpu_entry "$entry"
  echo "=== Applying configuration to GPU VM node $ip (gen $gen) ==="
  talosctl apply-config --insecure \
    --nodes $ip \
    --file worker.yaml \
    --config-patch @worker-gpu-vm.patch.yaml \
    --config-patch "{\"machine\":{\"nodeLabels\":{\"spritz.dev/gpu-gen\":\"$gen\"}}}"
  echo "Configuration applied to $ip"
  echo ""
done
