# Investigation: Worker Storage, Scratch Workers and the Talos 1.14 Path (2026-09)

This is the reasoning behind two changes: the worker template fix that puts EPHEMERAL on the NVMe, and
the NVMe scratch workers. The work started as "add two worker nodes with 128 GB of NVMe
for storage-hungry, ephemeral workloads (GitHub runners and the like)". It turned into the
discovery that the existing workers' NVMe had been doing nothing for a month.

Anything topological (node names, addresses, which node was the pilot) is tracked in the private
infrastructure repo, per the public/private split in `AGENTS.md`.

## Summary

- **The existing workers' Optane NVMe was unused.** All of `/var` (containerd, kubelet, pod
  logs, emptyDir, local-path volumes) was on the SD cards. The Optane exists precisely to keep
  those writes off the SD cards.
- **Cause:** the worker machine config was regenerated from scratch during the August outage
  recovery, and that dropped the disk layout and the kubelet image-GC tuning. See `AGENTS.md`
  Gotchas #4.
- **Fix:** every worker now puts its whole EPHEMERAL volume on "the NVMe" through one
  `VolumeConfig` in the shared template. The SD card is boot-only. That same definition covers
  the new 128 GB scratch workers, so they differ from the rest only by a label and a soft taint.
- **Upgrades are a separate problem.** The workers are Raspberry Pi CM5 Lites that stock Talos
  can't yet boot from SD, so every worker upgrade (including to Talos 1.14) goes through a pinned
  community image. That's separate work (the CM5 upgrade path), with its own PR and runbook.

## What we found

### Evidence that the Optane was idle

- **kubelet stats.** `kubectl get --raw /api/v1/nodes/<node>/proxy/stats/summary` reported the
  kubelet filesystem and the image filesystem as **the same ~27.5 GiB filesystem** on every
  worker. That's the SD card's EPHEMERAL partition, 54–62 % full.
- **talosctl.** `talosctl get volumestatus` put EPHEMERAL on the SD card. `get discoveredvolumes`
  showed the Optane still split into the 9.0 GB and 5.4 GB xfs partitions from its original
  setup, with nothing mounting them.
- **Live config.** `talosctl get machineconfig` on every worker had **no `machine.disks` at all**,
  although the template in this repo still declared it.

### How it happened

1. **Spring:** the original worker config mounted the Optane as two partitions (9 GB at
   `/var/lib/containerd`, the rest at `/var/lib/kubelet`), and was applied then.
2. **The August outage recovery:** the worker config was regenerated in an untracked working
   copy, which dropped every non-default setting. The regenerated file was applied to the
   workers.
3. **Later:** a consolidation elsewhere committed the regenerated file as "the corrected one" and,
   looking at the live nodes, described the Optane as untouched. That was true of the nodes at the
   time, but wrong about what had been intended.

After that, three copies of the worker config disagreed:

| Copy | Optane mounts | Image GC 75/55 |
|---|---|---|
| This repo's template (canonical) | yes (legacy split) | yes |
| A superseded copy outside this repo | no | yes |
| Live workers | no | no |

Re-applying this repo's template would have **brought back** the legacy split and mounted a 9 GB
partition over roughly 14 GiB of live containerd state. So the one copy that looked correct was the
dangerous one to apply.

### Other drift found by diffing the template against live

We compared the rendered template with `talosctl get machineconfig` structurally, with secrets masked:

- **Image GC thresholds:** in the template, absent live. Kept, and they now matter more.
- **`cluster.network.cni: none` / `cluster.proxy.disabled`:** in the template, absent live on the
  workers. They're inert there, because the control plane (where both are set) deploys the CNI and
  kube-proxy manifests. We confirmed that no flannel or kube-proxy is running.
- **A `kernel.modules` block (btrfs, vc4):** a PR describing itself as "prose only" had uncommented
  it. It never reached a node, but it would have been applied next time. Re-commented.

## Decisions

### EPHEMERAL on the NVMe, SD for boot only

- **Why this and not restoring the old split.** The old split moved only containerd and kubelet;
  logs, emptyDir and local-path stayed on the SD. It used the legacy `machine.disks` path, which
  Talos is retiring. And its 9 GB containerd partition could no longer hold the node's containerd
  state.
- **Why the whole volume fits on 14 GB.** Images actually in use are about 2–3 GiB compressed per
  worker, and the 75/55 image-GC thresholds keep the cache well below the ceiling.
- **Why not boot from NVMe.** It's an untested boot path on CM5 Lite (EEPROM order plus overlay
  support), and it gains almost nothing once `/var` is off the SD.
- **No fallback, on purpose.** A worker whose NVMe is dead waits for a matching disk rather than
  quietly going back to wearing out its SD card.
- **Placement is fixed at creation.** Existing workers are moved one at a time, following
  [`runbook-worker-ephemeral-relocation.md`](runbook-worker-ephemeral-relocation.md): drain, apply,
  clear the NVMe, wipe EPHEMERAL, verify. The runbook's final check is the outcome itself, the SD's
  write rate from `/proc/diskstats` before and after, not the config.

### Consistent or tailored? Consistent, plus one small patch

The scratch workers are the same hardware with a bigger NVMe, so they get the same image, the same
template and the same storage rule. What differs is *scheduling intent*, and that's all the patch
(`bootstrap/talos/worker-nvme.patch.yaml`) holds:

- **Label** `athena.asn.casa/scratch=nvme`. Scratch workloads select it and size their scratch
  with `emptyDir.sizeLimit`, which XFS project quotas enforce (`diskQuotaSupport` is on).
- **Taint** with `PreferNoSchedule`, a soft one. General pods avoid these nodes but can still use
  them when the other workers are full, for example while a worker is drained for the relocation
  runbook. A hard taint would reserve the disk but waste two nodes' CPU and RAM most of the time.

## Talos 1.14 readiness

v1.14.1 is current. The following was checked against the v1.14.0 release notes rather than
from memory.

- **Stock installer image.** `ghcr.io/siderolabs/installer` is no longer published. Upgrades use an
  Image Factory image or an equivalent. It was already the wrong installer for the CM5 workers,
  since it has no board overlay and no extensions. The control-plane VMs (generic arm64) still
  name it too and need moving to Image Factory before 1.14. That's tracked in the private
  tracker, not here.
- **Upgrade path.** Talos recommends the latest patch of each intermediate minor. The CM5 image
  source constrains the exact hops. They're decided in the CM5 upgrade-path work, not here.
- **Deprecated but still accepted.** `machine.nodeLabels`/`nodeTaints` (now `KubeNodeConfig`),
  `machine.install` (now `UnattendedInstallConfig`), `machine.kubelet` (now `KubeletConfig`),
  `features.kubePrism`/`hostDNS`, and much of `cluster.*`. Migrate after the upgrade. The scratch
  patch uses the old fields because the workers run 1.13.
- **Workload isolation (`SecurityProfileConfig`).** It's driven by config: upgraded clusters, and
  nodes applied from templates without that document, keep the old behaviour. Enabling it later
  breaks in-tree iSCSI. Nothing in this repo uses iSCSI, so the `iscsi-tools` extension looks
  unneeded.
- **Dedicated CRI/KUBELET/LOG/ETCD partitions** are new in 1.14, and the choice is fixed at
  provisioning. We don't need them; everything in EPHEMERAL on the NVMe is the goal.
- **No impact:** the `apply-config --mode=reboot` removal (unused here) and the etcd metrics port
  move (we don't scrape etcd). After the upgrade, confirm that Cilium is happy with NRI enabled and
  with TLS 1.3 on the apiserver.

## CM5 Lite image status

> **Superseded (2026-09-26):** upstream Talos v1.14.1 from Image Factory now boots a CM5 Lite from
> SD. See [`investigation-2026-09-cm5-upstream-talos.md`](investigation-2026-09-cm5-upstream-talos.md).
> The notes below are kept as the record of what was known before that test.

- **Upstream.** `siderolabs/sbc-raspberrypi` supports Pi 5, but a CM5 Lite doesn't bring its SD card
  up (`#98`, open and stalled; the maintainer suspects DTB selection). `v0.2.2` fixed CM5 Ethernet,
  not SD, and `#97` is about Pi 5 D0 u-boot.
- **Community builds.** The live workers were traced to a community v1.13.2 build that has had no
  releases since. Choosing and pinning a maintained source, and proving upgrade and rollback on a
  spare CM5 Lite that boots from SD like the fleet, is the separate CM5 upgrade-path PR and
  its runbook.
  The new scratch workers boot from SD like the rest, so either one can serve as that spare before
  it takes workloads.

## Lessons (general form in `AGENTS.md` Gotchas #4)

- A regenerated config drops everything that isn't a default. After a regeneration, diff it
  against the last known-good config before applying it.
- Keep exactly one source of truth, and diff it against the live nodes before trusting it.
- A PR's description of itself ("prose only") is a claim; read the diff.
- Verify the outcome, not the config. For SD protection the evidence is the card's write rate, and
  "the VolumeConfig is present" doesn't show that.
