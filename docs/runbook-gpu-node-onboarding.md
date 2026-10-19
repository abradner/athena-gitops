# Runbook: onboarding a GPU worker (Intel iGPU, spritz video transcoder)

GPU workers are amd64 desktops with an Intel iGPU, joined as tainted workers so
that only the spritz transcoder (and cluster DaemonSets) run on them. The first
was an HP ProDesk 600 G3 SFF (i7-7700, HD 630, Gen 9.5), onboarded 2026-10-17.
Expected next: Skylake (Gen 9), Comet Lake (Gen 9.5) and Ice Lake (Gen 11).
Every one of them uses the same schematic, the same patch, and a per-node
generation label.

| Piece | Where |
|---|---|
| Image Factory schematic | `bootstrap/talos/worker-gpu.schematic.yaml` |
| Machine-config patch (storage, swap, taint, installer) | `bootstrap/talos/worker-gpu.patch.yaml` |
| Node list, `<ip>:<gen>` | `GPU_WORKER_IP` in `bootstrap/athena.zsh` (gitignored; template in `athena.template.zsh`) |
| Device plugin | `cluster/core/intel-gpu-plugin/` |
| Transcoder | `cluster/apps/spritz-{staging,production}/spritz/transcoder.yaml` |

## Hardware layout

The layout is decided once for the whole class; the reasons are in the patch's
header.

- **Small NVMe (Optane, 14 GB):** the Talos install plus swap on the rest of
  the disk, with zswap in front of it.
- **SATA SSD:** EPHEMERAL, i.e. all of `/var`. The transcoder's `emptyDir` must
  hold an original of up to 4 GiB plus its outputs, which the Optane cannot.
- **No HDD.** Take one out if the box has it: nothing uses it, and it draws
  power.

Selectors match transport and rotation, never device names, so a different-sized
SSD still works. A box with a second NVMe or a second SATA SSD would match more
than one disk: rethink the selectors before onboarding it.

## 1. Firmware

- **UEFI, Secure Boot on.** The schematic's ISO and installer are the
  `-secureboot` variants. The plain installer writes an unsigned bootloader that
  the firmware skips, and the node then PXE-boots. That is what happened to the
  first node.
- **Boot order:** the NVMe first, then USB. The Talos ISO halts when it finds an
  installed system (`talos.halt_if_installed=1`), so pull the stick after
  installing.
- **Intel AMT** (budget AMT, no KVM; serial-over-LAN is the only remote
  console). In MEBx (Ctrl-P at POST): set the password, Activate Network Access,
  enable SOL/Storage Redirection, and on AMT 11+ enable **Legacy Redirection
  Mode** (amtterm uses 16994 without TLS). The schematic sends the kernel
  console to `ttyS4` (the AMT "KT Redirection" UART, PCI `00:16.3`). Run
  `amtterm` yourself: it prompts for the AMT password.

## 2. Boot media

Get the schematic id (it is content-addressed, so the same file always returns
the same id):

```bash
curl -sS -X POST --data-binary @bootstrap/talos/worker-gpu.schematic.yaml https://factory.talos.dev/schematics
```

**Never copy another node class's schematic, and never add an extension
"because it is harmless".** An extension that ships a service (cloudflared,
tailscale, nut-client, zfs, and others) waits for its own config, or for zfs
for `/dev/zfs`, and never comes up without it. Boot's `startAllServices` task
waits for every service, so the machine stays in stage `booting`, and after
Talos's `BootTimeout` (70 min) the boot fails and the node reboots. The first
GPU schematic was copied from another one with those four in it, and the first
node rebooted every ~71 minutes for two days, evicting the transcoders each
time, while the kubelet looked Ready throughout. The schematic lists only
`i915`, `intel-ucode` and `mei`; add one only with the config it needs.

Write `https://factory.talos.dev/image/<id>/<talos version>/metal-amd64-secureboot.iso`
to a USB stick and boot it. If the schematic or the Talos version changes,
update `install.image` in `worker-gpu.patch.yaml` to match.

## 3. Address

Give the node a DHCP reservation inside the worker telemetry band
(`10.10.80.110`–`.139`; see "Worker addresses are not a free choice" in
`bootstrap/README.md`). Add it to `GPU_WORKER_IP` as `<ip>:<gen>`, for example
`"10.10.80.127:9.5"`, and update the 1Password backup of `athena.zsh`.

## 4. Check the node in maintenance mode

```bash
talosctl -n <ip> -e <ip> get disks --insecure
talosctl -n <ip> -e <ip> get discoveredvolumes --insecure
talosctl -n <ip> -e <ip> get securitystate --insecure   # secureBoot: true
```

Write down the model and serial of each disk and confirm them with Alex before
wiping. Old partitions (a Windows install, a previous EPHEMERAL) stop volumes
from provisioning, so wipe the NVMe and the SSD:

```bash
talosctl -n <ip> -e <ip> wipe disk nvme0n1 <ssd> --method FAST --insecure
```

## 5. Apply

`apply-worker.sh` applies every list, so for one new node run its command by
hand:

```bash
cd bootstrap/talos
talosctl apply-config --insecure -n <ip> -e <ip> \
  --file worker.yaml \
  --config-patch @worker-gpu.patch.yaml \
  --config-patch '{"machine":{"nodeLabels":{"spritz.dev/gpu-gen":"<gen>"}}}'
```

**Before applying, check that the hydrated `worker.yaml` matches the
template.** On 2026-10-17 it dated from 6 Sep and still carried the legacy
Optane split (`machine.disks`) and the `btrfs`/`vc4` modules. Compare with
the secrets ignored:

```bash
diff <(grep -v '{{' worker.template.yaml | grep -vE '^\s*#|^\s*$') <(grep -vE '^\s*#|^\s*$' worker.yaml)
```

Only lines holding secrets may differ.

## 6. Taint it by hand, once

Talos cannot add a taint to a node that has already registered: the API server's
NodeRestriction refuses it (`node "<name>" is not allowed to modify taints`). That
error also stops Talos applying the labels. As soon as the node appears:

```bash
kubectl taint node <name> spritz.dev/gpu=intel:NoSchedule
```

Talos then applies the labels by itself.

## 7. Verify the outcome

- **The boot finished:** `talosctl -n <ip> get machinestatus -o yaml` shows
  `stage: running`, not `booting`, and `talosctl -n <ip> services` lists no
  service stuck in `Waiting`. A Ready kubelet is not proof: the node is Ready
  long before the boot sequence gives up and reboots it (see section 2). Check
  again after 75 minutes that it has not rebooted (`talosctl -n <ip> get
  machinestatus` keeps its `created` time).
- `kubectl get node <name> -o wide` shows Ready, amd64, and the taint, plus the labels
  `spritz.dev/gpu=intel` and `spritz.dev/gpu-gen=<gen>`.
- `talosctl -n <ip> get volumestatus`: EPHEMERAL is on the SSD, `s-swap` on the
  NVMe; `talosctl -n <ip> read /proc/swaps` lists it.
- `talosctl -n <ip> dmesg | grep -iE 'i915|ttyS4'`: i915 found the GPU and loaded
  its DMC firmware without errors, and `ttyS4` sits at `00:16.3`.
- `kubectl get node <name> -o jsonpath='{.status.allocatable}'` includes
  `gpu.intel.com/i915: 4` (`-shared-dev-num=4`). A Ready plugin DaemonSet is not proof on its own.
- **The transcoder's preflight is the real proof.** Each boot does a real
  0.1 s HEVC and H.264 encode, and exits 78 (crash-loops visibly) if the
  node cannot encode. On a new generation, especially Skylake, run a transcoder
  pod pinned to the node (`nodeSelector: spritz.dev/gpu-gen: <gen>`) and read
  its log for `transcoder up (encoder=vaapi`.

## Changing the schematic or Talos version on a running node

The installer in `worker-gpu.patch.yaml` is the one to upgrade with. Upgrade
one GPU node at a time: while there is only one, the transcoders are down from
the drain until the node is back and the device plugin has registered (a few
minutes), so pick a quiet moment and tell whoever is promoting spritz.

talosctl cannot drain a worker by itself: its client-side drain fetches a
kubeconfig from the node it is upgrading, and a worker refuses (`kubeconfig is
only available on control plane nodes`). It then installs the new image and
skips the drain AND the reboot, still exiting 0. So install without a reboot,
drain with kubectl, and reboot through Talos:

```bash
talosctl -n <ip> -e <ip> upgrade --no-reboot \
  --image factory.talos.dev/metal-installer-secureboot/<schematic id>:<talos version>
kubectl drain <name> --ignore-daemonsets --delete-emptydir-data --timeout=5m
talosctl -n <ip> -e <ip> reboot --mode powercycle --wait
kubectl uncordon <name>
```

Use a plain upgrade, not `--stage`: a plain upgrade may take over a boot
sequence that is still running (for example one stuck as in section 2), a
staged one may not. `--mode powercycle` boots through the firmware, so the
node comes up on the new signed UKI rather than kexec-ing. The drain also
deletes any Failed transcoder pods left from earlier reboots.

Then bring the node's own config in line, so it does not keep naming the old
installer. It must be a strategic-merge patch: the config is multi-document,
and talosctl refuses JSON6902 patches on it.

```bash
talosctl -n <ip> -e <ip> patch mc --mode no-reboot \
  --patch '{"machine":{"install":{"image":"<the same image>"}}}'
```

Check `talosctl -n <ip> get extensionstatuses` lists the new schematic id, then
verify as in section 7. On the first node (2026-10-19) the transcoders were
down about 2.5 minutes, from the drain to `transcoder up`.

## After a reboot: Failed transcoder pods are expected

When a GPU node reboots, the kubelet re-admits the pods it had before the
device plugin has registered again, and rejects the old transcoder pod:
`UnexpectedAdmissionError ... no healthy devices present; cannot allocate
unhealthy devices gpu.intel.com/i915`. The Deployment immediately makes a
replacement, which schedules once the plugin is up. Kubernetes does not
garbage-collect Failed pods until there are thousands of them, so the rejected
ones stay listed (`Unknown`/`Failed`). They are harmless; delete them by name.

If they keep coming back, the node is rebooting by itself: check that its stage
is `running` (section 7). On the first node, from 2026-10-17 to 2026-10-19,
they were the trace of the 70-minute boot-timeout reboots described in
section 2, not of one-off reboots.

## Why VA-API and not QSV

The transcoder runs `TRANSCODER_ENCODER=vaapi`. QSV in the image needs the
oneVPL GPU runtime (`libmfx-gen`), which supports only Gen 12+. On the first
node (Gen 9.5) the QSV preflight fails with `Device creation failed` and exits
78. VA-API through the iHD driver covers Gen 8+, so it works on every node in
the fleet (spritz #1092, release `v202610170753`).
