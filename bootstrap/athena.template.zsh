# Node IPs and cluster vars for the Athena homelab.
#
# Copy this file to athena.zsh (gitignored) and fill in real values:
#   cp athena.template.zsh athena.zsh
#
# athena.zsh is sourced by bootstrap/talos/apply-*.sh and bootstrap/mise.toml.
# Without it a fresh clone cannot run the disaster-recovery playbook, so keep
# the real copy backed up (e.g. in the same 1Password vault as the Talos
# secrets note).

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

# The control plane node used for `talosctl bootstrap` and kubeconfig retrieval
BOOTSTRAP_NODE="${CONTROL_PLANE_IP[1]}"

# Point talosctl at the hydrated (gitignored) talosconfig
export TALOSCONFIG="${0:a:h}/talos/talosconfig.yaml"
