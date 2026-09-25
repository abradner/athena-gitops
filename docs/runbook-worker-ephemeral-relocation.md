# Runbook: Move a Worker's EPHEMERAL Volume onto its NVMe

For an **existing** worker whose EPHEMERAL volume (`/var`: containerd,
kubelet, pod logs, emptyDir, local-path PVs) still lives on the SD card.
New workers don't need this: they get the placement at install from the
`VolumeConfig` at the end of
[`bootstrap/talos/worker.template.yaml`](../bootstrap/talos/worker.template.yaml).

## Why this exists

The SD card should hold only BOOT/STATE/META, which are rarely written.
Every other write belongs on the NVMe, and that's the reason the workers
have one. Talos decides EPHEMERAL's placement **only when the volume is
created**. Applying the `VolumeConfig` to a running node records the intent
but moves nothing. Moving it means wiping EPHEMERAL so that Talos creates
it again, this time on the disk the selector matches.

How the workers ended up on the SD card is in AGENTS.md Gotchas #4.

## Before you start

- **One node at a time.** Don't start the next one until the previous one
  is `Ready`, uncordoned, and verified.
- **Never in the same window as a Talos upgrade.** Keep the two changes
  separate so a failure has one cause.
- **Capacity:** the other workers must be able to absorb a drained node's
  pods.
- **Data on the node is destroyed.** Everything in EPHEMERAL goes, including
  local-path PVs pinned to this node. List them first and make sure every
  one is disposable (a buffer or cache) or backed up:

  ```bash
  kubectl get pv -o custom-columns='PV:.metadata.name,NAMESPACE:.spec.claimRef.namespace,CLAIM:.spec.claimRef.name,NODE:.spec.nodeAffinity.required.nodeSelectorTerms[0].matchExpressions[0].values[0]' | grep <node-name>
  ```

- A hydrated `worker.yaml` rendered from the current template (see
  `bootstrap/README.md`). Check that it has the EPHEMERAL `VolumeConfig`.

## Pre-flight (read-only)

```bash
talosctl -n <worker-ip> get volumestatus EPHEMERAL      # expect LOCATION /dev/mmcblk0p*
talosctl -n <worker-ip> get discoveredvolumes | grep nvme
talosctl -n <worker-ip> read /proc/diskstats | awk '$3=="mmcblk0"{print "sectors written:", $10}'
```

Record the sector count and the time. Take it again after 15 minutes. That
gives the SD's write rate *before* the move, the baseline the final check
compares against. The original workers' Optanes still carry two stale xfs
partitions (about 9.0 + 5.4 GB) from the old `machine.disks` layout. Record
their device names from the `discoveredvolumes` output, and confirm they are
exactly those two: xfs, no label, the expected sizes, and nothing else on the
NVMe. The wipe below uses the names you recorded; don't assume numbering.
If the NVMe holds anything else, stop.

## Procedure

1. **Drain.**

   ```bash
   kubectl cordon <node-name>
   kubectl drain <node-name> --ignore-daemonsets --delete-emptydir-data
   ```

2. **Apply the config with the `VolumeConfig`.** EPHEMERAL stays where it
   is for now. The node may reboot to apply other differences between the
   template and the live config (image-GC tuning, for one). It's drained, so
   that's harmless.

   ```bash
   talosctl -n <worker-ip> apply-config --file bootstrap/talos/worker.yaml
   talosctl -n <worker-ip> get volumeconfigs EPHEMERAL -o yaml   # selector now names nvme
   ```

3. **Clear the NVMe.** Skip this on a disk that has no partitions.
   Provisioning needs free space, and the stale partitions fill the disk.

   ```bash
   # the partition names you recorded and verified in pre-flight
   # (on the original workers: typically nvme0n1p1 and nvme0n1p2)
   talosctl -n <worker-ip> wipe disk <stale-partition-1> <stale-partition-2> --drop-partition
   talosctl -n <worker-ip> get discoveredvolumes | grep nvme   # only nvme0n1 left
   ```

4. **Wipe EPHEMERAL and reboot.** Talos creates it again on the NVMe.

   ```bash
   talosctl -n <worker-ip> reset --system-labels-to-wipe EPHEMERAL --reboot
   ```

   *Unproven until the pilot:* `--system-labels-to-wipe` wipes the partition
   but doesn't say it removes it. If Talos finds the SD's old `EPHEMERAL`
   partition by its label and reuses it, step 5 shows EPHEMERAL back on
   `mmcblk0`. In that case, drain again, drop that partition with
   `talosctl -n <worker-ip> wipe disk <mmcblk0 EPHEMERAL partition> --drop-partition`,
   and repeat this step. Record which way it went.

5. **Verify.** The config alone doesn't prove anything; check each of these.

   ```bash
   talosctl -n <worker-ip> get volumestatus EPHEMERAL      # LOCATION /dev/nvme0n1p*
   kubectl get node <node-name> -o jsonpath='{.status.capacity.ephemeral-storage}'
   ```

   - The node's ephemeral-storage matches the NVMe (about 13 GiB on an
     Optane), not the SD (about 28 GiB).
   - The node is `Ready`, and Cilium and the log shipper are running on it.
   - `talosctl -n <worker-ip> get resolvers` shows `searchDomains: []`
     (AGENTS.md Gotchas #5). On 1.13 that holds whatever DHCP sends. It
     starts to matter at 1.14, which applies DHCP search domains, so this is
     a cheap baseline for later.
   - Repeat the SD write-rate sample from the pre-flight. The rate should
     be close to zero. That is the result this runbook is for.

6. **Uncordon.**

   ```bash
   kubectl uncordon <node-name>
   ```

## If it goes wrong

- **EPHEMERAL doesn't come up after step 4.** Run
  `talosctl -n <worker-ip> get volumestatus EPHEMERAL -o yaml`, which shows
  why. The usual causes are no free space on the NVMe (step 3 was skipped)
  or a dead or missing NVMe. The selector deliberately has no fallback, so
  Talos waits rather than going back to the SD.
- **Back to the SD (rollback).** Apply a `worker.yaml` without the
  `VolumeConfig` document, then repeat step 4. EPHEMERAL is recreated at the
  Talos default location on the install disk. *Not yet exercised.*
- **Last resort.** Reflash the SD card with the same image the other workers
  run and apply the config in maintenance mode, as for a new worker
  (`bootstrap/README.md` §5). The node rejoins as a fresh node. Clear the
  NVMe first (step 3, from maintenance mode): an original worker's Optane
  still carries the stale partitions, and with no free space EPHEMERAL has
  nowhere to go.

## Record of runs

The first run is the pilot, and it's also where the unverified parts of this
runbook get tested. That includes the `wipe disk --drop-partition` step and
what happens to the SD's old EPHEMERAL partition. Write down what you
actually observe below, and correct the steps above wherever reality
differed.

| Date | Node | Result | Notes |
| --- | --- | --- | --- |
| | | | |
