# Node addresses and cluster vars for zagreus, the CI build cluster.
#
# Copy to zagreus.zsh (gitignored) and fill in real values:
#   cp zagreus.template.zsh zagreus.zsh
# The real addresses live in the private infrastructure repo; this repo is
# public. Sourced by bash (talos/apply-*.sh) and zsh, so keep the syntax
# portable and never index into the arrays (bash is 0-indexed, zsh 1-indexed).

# Control plane (CM5 4 GB, Optane). One node for now; HA adds two more.
CONTROL_PLANE_IP=(
  "<ip>"
)

# arm64 runner nodes (CM5 8 GB + 128 GB NVMe), worker-arm-runner.patch.yaml.
ARM_RUNNER_IP=(
  # none yet; ARM_RUNNER_IP=() is fine
)

# arm64 image builder (CM5 8 GB + USB SSD), worker-arm-builder.patch.yaml.
ARM_BUILDER_IP=(
  "<ip>"
)

# amd64 runner nodes (ProDesk/EliteDesk), worker-amd64.patch.yaml. An entry
# may be "<ip>:<path to extra patch>" for a box with more disks to place.
AMD64_RUNNER_IP=(
  "<ip>"
)

BOOTSTRAP_NODE="<ip>"

_zagreus_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
export TALOSCONFIG="${_zagreus_dir}/talos/talosconfig.yaml"
