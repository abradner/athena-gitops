# Runbook: arming the node watchdogs

Every worker arms its hardware watchdog through Talos
(`bootstrap/talos/watchdog.patch.yaml`): machined feeds it, and if machined
stops feeding it for two minutes the hardware resets the node. Before this
nothing on the cluster reset a hung node: Talos's kernel has no soft or hard
lockup detector, and a hang without a panic stayed hung until someone power
cycled the box.

What a watchdog does NOT cover:

- **A kernel panic.** Talos boots with `panic=10`; a panic reboots by itself.
- **A power loss.** Nothing on the node survives it to act. The long-lived node
  health record (kept outside the cluster, in the private infra repo) is what
  shows those: when, how long, and the firmware's own reset cause.

The node inventory (which node has which device, and the hypervisor-side
watchdog for the control planes) lives in the private infra repo, per the
public/private split.

## Which class has what

| class | device at `/dev/watchdog0` | how it is armed |
|---|---|---|
| CM5 and Pi 5 workers (plain and scratch) | `bcm2835` SoC watchdog | `watchdog.patch.yaml` |
| GPU worker VM (q35) | `iTCO_wdt`, QEMU's ICH9 TCO emulation, present on every q35 VM | `watchdog.patch.yaml` |
| GPU worker, bare metal | `iTCO_wdt` (the chipset's) | `watchdog.patch.yaml` |
| Control-plane VMs (arm64 `virt`) | **none**: Talos's arm64 kernel builds neither `i6300esb` nor `softdog`, and `virt` has no SBSA watchdog | outside the guest, on the hypervisor |

`apply-worker.sh` adds the patch for every worker class, so a fresh install is
armed. Running nodes are patched in place, with no reboot (below).

## Rollout (needs the operator's word)

One node at a time, least critical first: a scratch worker, then the other
scratch worker, then the CM5 workers, then the GPU VM. From
`bootstrap/talos`, with `athena.zsh` sourced:

```bash
ip=<node>
talosctl -n $ip patch mc --patch @watchdog.patch.yaml --dry-run   # expect "Applied configuration without a reboot" and the one added document
talosctl -n $ip patch mc --patch @watchdog.patch.yaml
talosctl -n $ip get watchdogtimerstatus                            # DEVICE /dev/watchdog0, TIMEOUT 2m0s
talosctl -n $ip read /sys/class/watchdog/watchdog0/state           # active
```

Then wait ten minutes and check the node is still Ready, and that
node-exporter reports `node_watchdog_info{state="active"}` with
`node_watchdog_timeout_seconds` 120 for it, before the next node. An armed
watchdog that resets a healthy node shows within those ten minutes.

## Forced-hang test (one node per class; needs the operator's word)

Proves the hardware resets a hung node by itself. It hangs the kernel on
purpose: it panics with the panic reboot disabled (`kernel.panic=0`), so
without a working watchdog the node would stay hung until power cycled.

Pick a node whose workloads can move. Drain it first.

On the GPU VM, drain also stops the transcoders: athena has one GPU node, so
video transcoding is down for the test. Pick a quiet window with the floor,
and afterwards confirm the transcoder logs `transcoder up (encoder=vaapi)` and
the node reports `gpu.intel.com/i915: 4` allocatable. A warm reset of this
VM class with the iGPU passed through was tested in maintenance mode on
2026-10-22 (i915 initialised again after each `qm reset`); a reset of a
joined node, or one mid-encode, had not been.

```bash
node=<node name>; ip=<its address>
kubectl drain $node --ignore-daemonsets --delete-emptydir-data
talosctl -n $ip get watchdogtimerstatus                            # armed, or stop here
date                                                               # T0
# The image is already on every node (node-exporter) and has a shell, but it
# runs as `nobody`: a privileged container then has NO capabilities and every
# /proc/sys write is refused (gotcha 6). --custom makes it root.
echo '{"securityContext":{"runAsUser":0,"runAsGroup":0}}' > /tmp/debug-root.json
# The crash happens ONLY if panic=0 was written: without that, the kernel's
# own panic reboot (panic=10) would bring the node back and the test would
# prove nothing. If a write fails it restores panic=10 and exits.
kubectl debug node/$node -n kube-system --profile=sysadmin --custom=/tmp/debug-root.json \
  --image=quay.io/prometheus/node-exporter:v1.11.1 -- \
  /bin/sh -c 'echo 0 > /proc/sys/kernel/panic && echo 1 > /proc/sys/kernel/sysrq || { echo 10 > /proc/sys/kernel/panic; exit 1; }; sleep 5; echo c > /proc/sysrq-trigger || echo 10 > /proc/sys/kernel/panic'
# kube-system is exempt from Pod Security, which a privileged debug pod needs.
# Check the node actually went down (talosctl stops answering within ~10 s).
# If it keeps answering on the same boot ID, the crash never happened:
# kubectl -n kube-system logs <the node-debugger pod> says why.
# Delete the debugger pod NOW, while the node is hung: after a crash the
# kubelet can lose its record that the pod already ran, and would run it,
# and panic the node, again.
kubectl -n kube-system get pods -o name | grep "node-debugger-$node" | xargs kubectl -n kube-system delete --force --grace-period=0
until talosctl -n $ip version >/dev/null 2>&1; do sleep 5; done; date   # back
kubectl uncordon $node
```

Expected time from T0 to the node answering again:

- **CM5:** the reset comes within about 15 s of the panic plus the boot.
  Measured on a CM5 Lite scratch worker on 2026-10-23, twice: the kernel of
  the new boot started 15 s after the crash, and Talos answered again 31 to
  37 s after it; Talos re-armed the watchdog about 5 s into the new boot.
  The SoC watchdog's hardware limit is about 15 s, and the kernel's watchdog
  core pings it between machined's pings; a panicked kernel pings nothing,
  so the hardware fires at its own limit, before Talos's two minutes. Then a
  boot of a minute or two.
- **GPU VM (iTCO):** machined feeds every timeout/3 (40 s), the TCO timer is
  programmed to the full two minutes, and on a TCO v2 timer QEMU resets the
  VM on the SECOND expiry: about 200 to 240 s after the panic, then the boot.
  QEMU resets on expiry only if its ICH9 `noreboot` strap is clear, and the
  guest cannot see the strap (`iTCO_wdt` clears the bit it can see and loads
  either way). The strap is set by default on q35 machine types up to 7.2,
  so check the running machine type on the hypervisor first (read-only):
  `qm status <vmid> --verbose | grep running-machine` must show 8.0 or later.
  If nothing happens by ten minutes, iTCO does not reset this VM: stop and
  start the VM from the hypervisor, and switch the class to
  `watchdog: model=i6300esb,action=reset` (the patch file says how).
- **Not back at all:** a CM5 that does not return needs a physical power
  cycle; a VM, `qm stop` and `qm start` on its host.

Pass also needs the node's own report of why it reset. On a CM5,
`talosctl -n $ip read /proc/device-tree/chosen/bootloader/rsts | xxd -p`
has bit `0x20` set (a watchdog-driven reset; a power loss reads `00001000`).
Test the bit, not the value: the first watchdog reset after a cold start read
`00001020`, the power-on bit still set beside it, and the next one `00000020`. On
the VM there is no such report: `iTCO_wdt` always reports a `bootstatus` of 0
(the driver hard-codes it), so the pass there is the new boot time landing
inside the expected window with no reboot asked for. (If the class ever
moves to `i6300esb`, check whether its `bootstatus` reports WDIOF_CARDRESET,
32, after the first forced hang before relying on it.) Either way, the boot must
appear in the node health record.

After any forced hang, expect XFS log recovery on the node's EPHEMERAL volume
in `talosctl dmesg`: the hang was an unclean stop.

## Rollback

```bash
talosctl -n $ip patch mc --patch @watchdog-remove.patch.yaml
talosctl -n $ip get watchdogtimerstatus                            # empty
talosctl -n $ip read /sys/class/watchdog/watchdog0/state           # inactive
```

A rollback also trips the out-of-cluster alert for a watchdog that was armed
and is not now (`AthenaNodeWatchdogDisarmed`), for up to 30 days: silence it
for that node.

No reboot. The `state` read is the check that machined disarmed the device
when it closed it: if it still reads `active`, the node will reset when the
timeout runs out, so reboot it deliberately instead. Remove the
`--config-patch @watchdog.patch.yaml` lines from `apply-worker.sh` too if the
rollback is meant to survive the next install.
