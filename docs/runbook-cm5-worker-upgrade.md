# Runbook: Upgrading the CM5 Lite Talos Workers

How the metal workers get a new Talos version. The control plane is out of scope. Its
nodes are generic arm64 VMs and upgrade from Image Factory like any other Talos node.

**Every live upgrade or rollback needs the operator's explicit go-ahead, every time.**
Never run a hop in the same maintenance window as a storage or EPHEMERAL change. Two changes
in one reboot means you cannot tell which one broke the node.

## Why the workers are special

The workers are **Raspberry Pi Compute Module 5 Lite** boards that boot from SD (`mmcblk0`).
Stock Talos cannot boot them:

- Upstream `siderolabs/sbc-raspberrypi` fails SD init on a CM5 Lite with error -110. The fix
  is `siderolabs/sbc-raspberrypi#98`, which was still open on 2026-09-24. The suspected cause
  is DTB selection, and upstream warns the patch may cut SD throughput on boards that already
  work.
- `ghcr.io/siderolabs/installer` carries no board overlay and no extensions, and it is **not
  published at all from Talos 1.14**. **Never** point `talosctl upgrade` at it for a worker.
  Running `talosctl upgrade` without `--image` defaults to exactly that image, so
  always pass `--image`.

The workers therefore run a community Pi 5 build that uses the `raspberrypi/linux` vendor kernel.

## Image source

| | |
|---|---|
| Source | [`yama6a/talos-raspberry-pi5`](https://github.com/yama6a/talos-raspberry-pi5) |
| Why this one | Immutable per-build tags (`vX.Y.Z-N`), GitHub build attestations, a `build-inputs.json` recording the exact Talos / pkgs / kernel / overlay commits, and an SBOM. It ships `iscsi-tools` + `util-linux-tools`, the same pair the fleet already runs, so a hop does not change the extension set. The EFI partition carries the `bcm2712-rpi-cm5l-*` DTBs. |
| Caveat | Upstream says CM5 is "plausible but unverified", and there is no hardware test in its CI. The spare-node proof below is what makes it verified *here*. |
| Rejected | `ojsef39/talos-rpi5`: no attestations or input record, it ships gvisor instead of the fleet's extensions (a hop would silently drop `fstrim` and `iscsid`), and it has no 1.14.1. Building our own is possible (the yama6a repo builds on arm64 in about 40 minutes) and is the fallback if yama6a goes stale. |

### Where the fleet came from

Established 2026-09-24. Recorded so nobody re-derives it.

The fleet was flashed from **`johnlaur/talos-builder` v1.13.2**, asset `metal-arm64-rpi5.raw.xz`,
sha256 `8d2d2dad3eafab3d8779520fa0669f0b0f48d7d616d1af4c1958d1bdccb0d27b`. It was matched by
the asset digest, by Talos SHA `23154840` in the image's `init`, and by the kernel banner
`6.18.29-talos … #1 SMP Sun May 17 19:09:54 UTC 2026`. That source has published nothing since,
and its installer image cannot be pulled, so there is no upgrade path from it. Every hop below
also moves the node to the new source.

### Pinned images

Pin by immutable tag **and** digest. The bare `vX.Y.Z` tag moves on rebuilds.

| Hop | Installer |
|---|---|
| 1 | `ghcr.io/yama6a/talos-raspberry-pi5:v1.13.9-9@sha256:7d008485ea4ebdce07e4316cb1237feb140534cda0be038b74b5ea5ecd2739ab` |
| 2 | `ghcr.io/yama6a/talos-raspberry-pi5:v1.14.1-1@sha256:aa8b922aacb3e873a2c8137b18c846543451983781424e07166bed144d4fa644` |

The path is the latest patch of 1.13 this source publishes, then 1.14. 1.13.10 exists upstream,
but this source skipped it. Its
[release notes](https://github.com/siderolabs/talos/releases/tag/v1.13.10) list only security
hardening and bug fixes (Linux 6.18.48, etcd 3.6.14), with nothing touching the installer,
bootloader or upgrade flow. Mixing sources between hops is the worse trade.

`machine.install.image` in `bootstrap/talos/worker.template.yaml` holds the **next** hop. Bump it
in the PR that records each completed hop.

## Verify an image before using it

Do this for any new pin, not only these two:

```bash
IMG=ghcr.io/yama6a/talos-raspberry-pi5:v1.14.1-1
gh attestation verify oci://$IMG --owner yama6a      # must exit 0 (needs registry auth: docker login ghcr.io)
docker buildx imagetools inspect $IMG | grep Digest  # must equal the pinned digest
docker pull --platform linux/arm64 $IMG
c=$(docker create --platform linux/arm64 $IMG)
docker export $c | tar -t | grep -E 'cm5l|rpi5/u-boot.bin|vmlinuz.efi'   # CM5L DTBs, Pi 5 U-Boot, UKI
docker inspect $IMG --format '{{index .Config.Labels "alpha.talos.dev/version"}}'
docker rm $c
```

Before a **minor** hop, also read the Talos release notes. yama6a's
[`docs/upgrade.md`](https://github.com/yama6a/talos-raspberry-pi5/blob/main/docs/upgrade.md)
has a good grep list.

## Access

The talosconfig is not kept on disk. Fill **only** `bootstrap/talos/talosconfig.template.yaml`
from the Talos bootstrap secure note in 1Password (the item `OP_TALOS_ITEM_ID` names; see
`bootstrap/README.md`, and flatten the YAML to dotted keys). Do this by hand or with a one-off
script, **not** `render-talos`: that command hydrates every template into `bootstrap/talos/`,
including the machine configs that carry the full cluster PKI. Write the result outside the repo
with mode `0600`, point `TALOSCONFIG` at it, and delete it when you're done. If `render-talos` was
used anyway, delete every hydrated `bootstrap/talos/*.yaml` that isn't a `.template.yaml`. `talosctl` comes from `mise` in `bootstrap/`, and the worker
addresses from `bootstrap/athena.zsh`.

## Procedure

### 0. Prove it on a spare CM5 Lite first (once per source, and again on every minor)

Use a CM5 Lite that **boots from SD like the fleet** and is not yet carrying workloads. A node
that boots some other way does not exercise the SD path, which is the risk here.

```bash
W=<spare worker's Talos address>
NODE=<its Kubernetes node name>   # match $W against INTERNAL-IP in: kubectl get nodes -o wide
check() {                         # run after every boot
  talosctl -n $W version          # Tag + SHA
  talosctl -n $W get extensions   # iscsi-tools + util-linux-tools
  talosctl -n $W dmesg | grep -i -E 'mmc|sdhci' | tail   # no -110 / timeout errors
  kubectl wait --for=condition=Ready node/$NODE --timeout=10m
}
check                                               # baseline: record Tag + SHA
talosctl -n $W upgrade --image <hop 1 pin> --wait
check                                               # expect v1.13.9, SHA not 23154840
talosctl -n $W reboot --wait                        # a second boot on the new image
check
talosctl -n $W rollback                             # back to the previous A/B entry
check                                               # expect the baseline Tag + SHA again
```

**The rollback caveat.** `talosctl rollback` flips only the A/B boot entry. The EFI partition
(U-Boot, DTBs, `config.txt`) that the new installer's overlay wrote is **not** restored. So the
rollback step also proves that the old kernel still boots on the new firmware bits. If it
doesn't, the recovery is a reflash, not a rollback. That is why the proof runs on a spare.

Pass criteria: every `version` matches its expectation, the extensions are present throughout,
the node goes `Ready` in Kubernetes after each boot, and SD shows no init errors. Afterwards,
upgrade the spare forward again, and put it into service only when the fleet goes the same way.

### 1. Roll out one worker at a time

For each worker, only after the previous one is healthy:

```bash
W=<worker's Talos address>; NODE=<its Kubernetes node name>   # as in step 0
kubectl drain $NODE --ignore-daemonsets --delete-emptydir-data
talosctl -n $W upgrade --image <pin> --wait
check                              # the function from step 0
kubectl uncordon $NODE
kubectl get node $NODE             # Ready, correct kubelet version
```

(`talosctl upgrade` cordons and drains on its own too. The explicit drain surfaces PDB blocks
before the reboot, not during it.)

Stop the rollout at the first surprise. A single node on the old version is harmless. A second
node in an unknown state is not.

### 2. After each completed hop

- Bump `machine.install.image` in `worker.template.yaml` to the next pin, with the evidence in
  the PR.
- For the 1.14 hop, see the follow-ups below.

## Talos 1.14 follow-ups (not blockers)

An upgraded node keeps its existing v1alpha1 config and behaves as before. These are opt-in:

- `machine.install`, `nodeLabels`, `nodeTaints`, `kubelet`, `kubePrism` and `hostDNS` are
  deprecated in favour of multi-doc configs (`UnattendedInstallConfig`, `KubeNodeConfig`, …).
  They are still honoured.
- Filesystem trim: upgraded clusters get **no** `FilesystemTrimConfig` document, so the built-in
  periodic trim stays off until one is added.
- Workload isolation (sandboxd) is off on upgraded clusters until a `SecurityProfileConfig`
  document is added.
- `talosctl apply-config --mode=reboot` is gone.
