# Node IPs and cluster vars for the Athena homelab.
#
# Copy this file to athena.zsh (gitignored) and fill in real values:
#   cp athena.template.zsh athena.zsh
#
# athena.zsh is sourced by BOTH zsh (bootstrap/mise.toml) and bash
# (bootstrap/talos/apply-*.sh), so keep the syntax portable. In particular,
# never index into the arrays below: bash arrays are 0-indexed and zsh
# arrays are 1-indexed, so the same expression targets different nodes.
#
# Without this file a fresh clone cannot run the disaster-recovery playbook,
# so keep the real copy backed up (e.g. in the same 1Password vault as the
# Talos secrets note).

# Control plane node IPs, applied in order by apply-control.sh
CONTROL_PLANE_IP=(
  "10.10.80.x"
  "10.10.80.x"
  "10.10.80.x"
)

# Worker node IPs, applied in order by apply-worker.sh
WORKER_IP=(
  "10.10.80.x"
  "10.10.80.x"
)

# The control plane node used for `talosctl bootstrap` and kubeconfig
# retrieval. Set explicitly (see indexing note above).
BOOTSTRAP_NODE="10.10.80.x"

# Point talosctl at the hydrated (gitignored) talosconfig.
# Resolves relative to this file; works when sourced from bash or zsh.
_athena_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
export TALOSCONFIG="${_athena_dir}/talos/talosconfig.yaml"
