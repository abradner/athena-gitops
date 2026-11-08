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

# Check every GPU entry before applying anything, so a malformed one cannot
# leave the cluster half-applied.
for entry in "${GPU_WORKER_IP[@]}" "${GPU_VM_WORKER_IP[@]}"; do
  split_gpu_entry "$entry"
done

# ---- Render: every node's config is built and checked before ANY apply. ----
#
# Every worker class also gets watchdog.patch.yaml (the hardware watchdog;
# see that file). The control planes do not: they have no watchdog device.
#
# The patches are merged here, offline, with `talosctl machineconfig patch`,
# and the apply loops below send the rendered file with no --config-patch.
# That way the check runs on exactly the bytes each node receives.
#
# What the check guards: the worker ResolverConfig's literal `domains: []`,
# the DHCP search-domain override (AGENTS.md Gotcha #7). A 1.13 talosctl's
# patcher re-serializes the config and drops empty lists, so the override
# silently becomes "inherit DHCP", which from Talos 1.14 blackholes external
# lookups. A 1.14 client keeps it. Checking every rendered config first means
# a bad one reaches no node, rather than every node before it.
# scripts/test_apply_worker.sh covers this.
command -v yq >/dev/null || { echo "yq is required (the repo's mise.toml pins it)" >&2; exit 1; }

# Rendered configs hold the full cluster PKI: a private directory, owner-only
# files, removed on exit.
render_dir="$(mktemp -d)"
chmod 700 "$render_dir"
trap 'rm -rf "$render_dir"' EXIT
umask 077

# render <name> <patch>...: worker.yaml plus the patches -> $render_dir/<name>.yaml,
# then refuse unless its ResolverConfig still has the literal [].
render() {
  local out="$render_dir/$1.yaml" name="$1" domains
  shift
  local args=() p
  for p in "$@"; do args+=(--patch "$p"); done
  talosctl machineconfig patch worker.yaml "${args[@]}" --output "$out"
  chmod 600 "$out"
  domains="$(yq -o=json -I=0 'select(.kind == "ResolverConfig") | .searchDomains.domains' "$out")"
  if [ "$domains" != "[]" ]; then
    echo "$name: rendered ResolverConfig searchDomains.domains must be a literal [] (got: '${domains}')." >&2
    echo "A 1.13 talosctl drops it when patching; use the 1.14 client bootstrap/mise.toml pins. Nothing was applied." >&2
    exit 1
  fi
}

# hostname_patch <arch> <ip>: a HostnameConfig patch naming the node
# athena-<arch>-<last octet of its address>. A fixed name survives rebuilds
# (Talos's "auto: stable" name changes with the machine's identity, leaving a
# stale Node object every time), and the octet makes the name and the address
# map onto each other. Computed from athena.zsh, so no name is committed.
# Applying this to a node still on an auto name renames it: it registers as a
# new Node, and the old Node object has to be deleted by hand.
hostname_patch() {
  printf '{"apiVersion":"v1alpha1","kind":"HostnameConfig","hostname":"athena-%s-%s","auto":"off"}' "$1" "${2##*.}"
}

# One rendered file per node: the hostname differs.
for ip in "${WORKER_IP[@]}"; do
  render "worker-$ip" @watchdog.patch.yaml "$(hostname_patch arm64 "$ip")"
done
# Scratch workers: same base config plus a label. See worker-nvme.patch.yaml
# for why it is a patch.
for ip in "${NVME_WORKER_IP[@]}"; do
  render "scratch-$ip" @worker-nvme.patch.yaml @watchdog.patch.yaml "$(hostname_patch arm64 "$ip")"
done
# GPU workers: the generation label differs per node too.
for entry in "${GPU_WORKER_IP[@]}"; do
  split_gpu_entry "$entry"
  render "gpu-$ip" @worker-gpu.patch.yaml @watchdog.patch.yaml \
    "{\"machine\":{\"nodeLabels\":{\"spritz.dev/gpu-gen\":\"$gen\"}}}" "$(hostname_patch amd64 "$ip")"
done
for entry in "${GPU_VM_WORKER_IP[@]}"; do
  split_gpu_entry "$entry"
  render "gpu-vm-$ip" @worker-gpu-vm.patch.yaml @watchdog.patch.yaml \
    "{\"machine\":{\"nodeLabels\":{\"spritz.dev/gpu-gen\":\"$gen\"}}}" "$(hostname_patch amd64 "$ip")"
done

# ---- Apply ----

for ip in "${WORKER_IP[@]}"; do
  echo "=== Applying configuration to node $ip ==="
  talosctl apply-config --insecure \
    --nodes "$ip" \
    --file "$render_dir/worker-$ip.yaml"
  echo "Configuration applied to $ip"
  echo ""
done

for ip in "${NVME_WORKER_IP[@]}"; do
  echo "=== Applying configuration to scratch node $ip ==="
  talosctl apply-config --insecure \
    --nodes "$ip" \
    --file "$render_dir/scratch-$ip.yaml"
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
    --nodes "$ip" \
    --file "$render_dir/gpu-$ip.yaml"
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
    --nodes "$ip" \
    --file "$render_dir/gpu-vm-$ip.yaml"
  echo "Configuration applied to $ip"
  echo ""
done
