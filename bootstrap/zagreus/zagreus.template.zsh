# Node addresses and cluster vars for zagreus, the CI build cluster.
#
# Copy to zagreus.zsh (gitignored) and fill in real values:
#   cp zagreus.template.zsh zagreus.zsh
# The real addresses live in the private infrastructure repo; this repo is
# public. Sourced by bash (talos/apply-*.sh) and zsh, so keep the syntax
# portable and never index into the arrays (bash is 0-indexed, zsh 1-indexed).

# Control-plane nodes (CM5 4 GB, Optane), one entry each.
CONTROL_PLANE_IP=(
  "<ip>"
)

# arm64 runner nodes (CM5 8 GB + 128 GB NVMe), worker-arm-runner.patch.yaml.
ARM_RUNNER_IP=(
  # may be empty: ARM_RUNNER_IP=() is fine
)

# arm64 image builder (CM5 8 GB + USB-attached NVMe), worker-arm-builder.patch.yaml.
ARM_BUILDER_IP=(
  "<ip>"
)

# amd64 runner nodes (ProDesk/EliteDesk), worker-amd64.patch.yaml. An entry
# may be "<ip>:<path to extra patch>" for a box with more disks to place.
AMD64_RUNNER_IP=(
  "<ip>"
)

# Patches every node takes, control plane and every worker class, on top of
# its class config. They live in the private infrastructure repo because they
# name private addresses (shipping Talos's kernel and service logs to the log
# receiver, for one). Must be declared; NODE_PATCHES=() is allowed but means
# the nodes ship no logs off the box.
NODE_PATCHES=(
  "<path to the private repo>/zagreus/talos/logging.patch.yaml"
)

BOOTSTRAP_NODE="<ip>"

_zagreus_dir="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
export TALOSCONFIG="${_zagreus_dir}/talos/talosconfig.yaml"
