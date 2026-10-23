# zagreus bootstrap

zagreus is the CI build cluster: a Talos cluster of its own that runs GitHub
Actions jobs for private repositories through actions-runner-controller (ARC)
runner scale sets. It is deliberately not athena. CI executes third-party code,
so it sits on its own network with internet egress only, and athena stays
production-only. What runs where, and why: `cluster/zagreus/README.md`.
Addresses and inventory live in the private infrastructure repo.

## Node classes

| Class | Hardware | Config | Power |
|---|---|---|---|
| Control plane | CM5 4 GB Lite, Optane NVMe | `controlplane.template.yaml` | always on |
| arm64 runner | CM5 8 GB Lite, 128 GB NVMe | `worker.template.yaml` + `worker-arm-runner.patch.yaml` | always on |
| arm64 builder (`zag-arm64-02`) | CM5 8 GB Lite, NVMe SSD in a USB 3 enclosure (RTL9210B) | `worker.template.yaml` + `worker-arm-builder.patch.yaml` | always on |
| amd64 runner | HP ProDesk/EliteDesk, Optane + SATA SSD | `worker.template.yaml` + `worker-amd64.patch.yaml` | woken on demand |

Every node, whatever its class, also takes the patches listed in `zagreus.zsh`'s
`NODE_PATCHES`; the apply scripts add them. They live in the private
infrastructure repo because they name private addresses. See "Patches every
node takes" below.

All on Talos 1.14, ahead of athena: zagreus is the canary for Talos upgrades.
The CM5s boot the Image Factory schematic in `talos/worker-cm5.schematic.yaml`:
the official overlay plus the kernel argument `sdhci.debug_quirks2=4`, which
keeps SD at 3.3 V High Speed (without it, mainline's switch to
1.8 V fails on some carriers and the card never appears). Flash that schematic's v1.14.4
`metal-arm64.raw.xz`; its installer ref, pinned by digest only, is in the
templates. A self-built equivalent (abradner/talos-builder v1.14.4-cm5l.1) is
kept published as a fallback. The amd64
boxes install the Secure Boot factory image from `worker-amd64.schematic.yaml`.
Use the talosctl this directory's `mise.toml` pins (1.14): a 1.13 client's
patcher drops the ResolverConfig's empty search-domain list, and the apply
scripts refuse an older client.

## Order of operations

### 1. Before any node boots

- Addresses reserved on the CI VLAN for every node (infrastructure repo).
- `cp zagreus.template.zsh zagreus.zsh` and fill it in (gitignored; keep a copy
  in 1Password beside the secrets note).
- Secrets: `talosctl gen secrets` into a private temporary directory (it is
  the cluster PKI; never shared `/tmp`), then store its content as a
  1Password Secure Note (`zagreus-bootstrap-secrets`) together with
  `controlplane.ip1`, `controlplane.endpoint` (`https://<ip>:6443`) and the
  talosconfig admin cert/key (`talosconfig.admin.crt`/`.key`), and delete the
  directory:
  ```bash
  d=$(mktemp -d) && talosctl gen secrets -o "$d/secrets.yaml"   # mktemp -d is mode 0700
  # store it in 1Password, then:
  rm -rf "$d"
  ```
- Hydrate with tools-workflow's `render-talos`, pointed at this directory and
  that note. Both settings are plain configuration (`TALOS_TEMPLATE_DIR`,
  `OP_TALOS_ITEM_ID`), so no code change is needed; set them for the run
  (environment or tools-workflow's `.env`). Confirm which wins: an athena
  render into this directory, or a zagreus render into athena's, would
  silently write the wrong cluster's PKI.
  ```bash
  TALOS_TEMPLATE_DIR=<path>/bootstrap/zagreus/talos OP_TALOS_ITEM_ID=<note id> ./workflow render-talos
  ```
  Then check that no `{{` survived (the apply scripts refuse if one did), and
  that `clusterName: zagreus` is what was rendered.
- Before applying, validate the rendered configs, each worker class with its
  patch applied:
  ```bash
  (
    source ../zagreus.zsh
    np=(); for f in "${NODE_PATCHES[@]}"; do np+=(-p "@$f"); done
    umask 077; d=$(mktemp -d) || exit 1   # merged configs hold the cluster PKI
    trap 'rm -rf "$d"' EXIT
    talosctl machineconfig patch controlplane.yaml "${np[@]}" -o "$d/cp.yaml" &&
      talosctl validate --config "$d/cp.yaml" --mode metal --strict || exit 1
    for p in arm-runner arm-builder amd64; do
      talosctl machineconfig patch worker.yaml -p @worker-$p.patch.yaml "${np[@]}" -o "$d/w-$p.yaml" &&
        talosctl validate --config "$d/w-$p.yaml" --mode metal --strict || exit 1
    done
  )
  ```
  `validate` does not catch a lost `searchDomains.domains: []` (a 1.13
  patcher turns it into null and the config still validates); the apply
  scripts are the gate for that.

### 2. Control plane

Flash the CM5 Lite image to the SD card and boot. Wipe the Optane from
maintenance mode if it held anything (`talosctl wipe disk nvme0n1 --insecure`).
Then:

```bash
cd bootstrap/zagreus/talos
source ../zagreus.zsh          # BOOTSTRAP_NODE, TALOSCONFIG
./apply-control.sh
talosctl --nodes "$BOOTSTRAP_NODE" bootstrap
talosctl --nodes "$BOOTSTRAP_NODE" kubeconfig ~/.kube/config-zagreus --merge=false
```

Talos names the context `admin@zagreus`; `provision.sh` and the token loader
below refuse any context without `zagreus` in its name.

### 3. CNI and Argo CD

```bash
KUBECONFIG=~/.kube/config-zagreus bootstrap/zagreus/kubernetes/provision.sh
```

The control plane goes `Ready` once Cilium runs.

### 4. Secrets, then GitOps

zagreus reads a 1Password vault that holds only CI items (`Tooling - Github Runners`),
through a service account scoped to that vault alone. Never reuse athena's
token here.

```bash
export OP_SERVICE_ACCOUNT_TOKEN="$(op read 'op://<vault>/<item>/credential')"
EXPECT_CONTEXT=zagreus KUBECONFIG=~/.kube/config-zagreus bootstrap/kubernetes/load1p-account-token.sh
KUBECONFIG=~/.kube/config-zagreus kubectl apply -f cluster/zagreus/root.yaml
```

### 5. Workers

Boot each worker in maintenance mode, wiping a reused disk first. Until its
config is applied a node accepts an unauthenticated `apply-config --insecure`
from anyone who can reach port 50000, so do this only where no untrusted host
(another CI runner, say) can reach it. CM5s:
flash the CM5 Lite image to SD. ProDesks: boot the Secure Boot ISO for
`worker-amd64.schematic.yaml`. Then apply:

```bash
cd bootstrap/zagreus/talos
./apply-worker.sh
```

A box with more disks than the class patch places (for example two SSDs and
an HDD) takes an extra per-node patch from the infrastructure repo, named in
`zagreus.zsh` as `"<ip>:<patch path>"`. Without one, the EPHEMERAL selector
would match either SSD.

Taints are set by the kubelet at registration (`registerWithTaints`), not by
Talos's `nodeTaints`, which NodeRestriction refuses after a node registers.
Check each node: `kubectl get node --show-labels`, and
`talosctl -n <ip> get volumestatus EPHEMERAL` shows the intended disk.

### Patches every node takes

`NODE_PATCHES` in `zagreus.zsh` lists patches from the private infrastructure
repo that every node gets on top of its class config. The first one ships
Talos's own logs off the node: a `KmsgLogConfig` for kernel messages and a
`machine.logging` destination for Talos's service logs (machined, kubelet,
containerd, etcd, apid). It exists because a node lost its disk, and the
kernel and service logs that would have explained it were in-memory ring
buffers that a crash-looping containerd then overwrote. A log store outside
the cluster is the only place those lines survive what they describe.

On a running node these apply without a reboot. Check first, then apply, from
`bootstrap/zagreus/` with `zagreus.zsh` sourced:
```bash
source zagreus.zsh
for f in "${NODE_PATCHES[@]}"; do talosctl -n <ip> patch mc --dry-run --patch "@$f"; done   # "without a reboot"
for f in "${NODE_PATCHES[@]}"; do talosctl -n <ip> patch mc --patch "@$f"; done
talosctl -n <ip> get kmsglogconfigs
```
Runner pods are kept off private addresses by the cluster's network policy
(`cluster/zagreus/core/runner-network-policy.yaml`), since their traffic leaves
with the node's address: a job could otherwise write to the log receiver as
its node.

### 6. Prove it

A scale set is proven by a real job: a run whose job shows that scale set's
runner in the jobs API, not a listener showing `Running`. The scale-set names
(`zagreus-ci`, `zagreus-ci-x64`, …) are deliberately different from the legacy
runners' labels (`spritz-ci`), so a test job aimed at one cannot land on the
other.
