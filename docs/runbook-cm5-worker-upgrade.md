# Runbook: Moving the CM5 Lite Talos Workers to a New Talos Version

How the CM5 Lite workers get a new Talos version. The control plane is out of scope: its nodes
are generic arm64 VMs and upgrade from Image Factory like any other Talos node. So are the GPU
workers, whose patches pin their own installers (`docs/runbook-gpu-node-onboarding.md`).

**Every live rebuild, upgrade or rollback needs the operator's explicit go-ahead, every time.**
One node at a time. Never combine a version change with an unrelated storage change in one
window: two changes in one reboot means you cannot tell which one broke the node. (Moving
EPHEMERAL onto the NVMe as part of a rebuild is the one planned exception, below.)

## Why the workers are special

The workers are **Raspberry Pi Compute Module 5 Lite** boards that boot from SD (`mmcblk0`).
What boots them, by Talos version (Image Factory binds the overlay per Talos release):

| Talos (official, Image Factory) | Overlay | CM5 Lite | Basis |
|---|---|---|---|
| 1.13.0 – 1.13.10 | sbc-raspberrypi v0.2.0 | SD init fails (-110) | upstream report, siderolabs/sbc-raspberrypi#98. Not tested here |
| 1.14.0 | v0.2.1 | No Ethernet. Never a valid target | upstream commit `b1fe7bda` ("restore CM5 Ethernet initialization", in v0.2.2). Not tested here |
| 1.14.1, bare `rpi_5` schematic | v0.2.2 | Worked on the soak board | soaked from SD, 2026-09-25 (`docs/investigation-2026-09-cm5-upstream-talos.md`) |
| 1.14.4, bare `rpi_5` schematic | | **Boot-looped on some carriers**: the switch to 1.8 V SDR104 fails and the card never appears | zagreus bring-up, 2026-10 (`bootstrap/zagreus/README.md`) |
| 1.14.4, `worker-cm5.schematic.yaml` (`03fea4ec…`) | | **Works**: the SD stays at 3.3 V High Speed | zagreus's CM5s (2026-10) |

`03fea4ec…` is the `rpi_5` overlay plus the kernel argument `sdhci.debug_quirks2=4`, with no
extensions. Why the bare schematic boots on some boards and not others was not established.

Two installer defaults are wrong for these boards, so **always pass `--image`** to
`talosctl upgrade`:

- A 1.13 `talosctl upgrade` defaults to `ghcr.io/siderolabs/installer`, which has no board
  overlay and is not published from 1.14.
- A 1.14 `talosctl upgrade` defaults to the vanilla Image Factory schematic, which has no Pi
  overlay either.

## Mixed-version workers

CM5 workers can sit on two generations at once while a rollout is under way: some rebuilt on the
Factory image below, some still on the community build the fleet was first flashed with. Nothing
in the cluster depends on them matching, but treat the two as different machines:

- **Find out what a node runs before touching it:** `talosctl -n $W version` and
  `talosctl -n $W get extensions` (Factory nodes show only the virtual `schematic` entry with its
  id; the community build shows `iscsi-tools` and `util-linux-tools`). Never assume from the
  node's age or role.
- **One config fits both.** The template's `ResolverConfig` is harmless on 1.13 and required on
  1.14, and its `install.image` is only read by an upgrade or a fresh install.
- **Never `talosctl upgrade` a community-build node** (see "Upgrading in place"): rebuild it.
- **Keep the community image** while any node runs it: it is that node's recovery path. It is
  **`johnlaur/talos-builder` v1.13.2** (Raspberry Pi vendor kernel, `iscsi-tools` +
  `util-linux-tools`), asset `metal-arm64-rpi5.raw.xz`, sha256
  `8d2d2dad3eafab3d8779520fa0669f0b0f48d7d616d1af4c1958d1bdccb0d27b`, identified (2026-09-24) by
  the asset digest, by Talos SHA `23154840` in the image's `init`, and by the kernel banner
  `6.18.29-talos … #1 SMP Sun May 17 19:09:54 UTC 2026`. That source has published nothing since,
  and its installer image cannot be pulled.

Which node runs what, with addresses, is recorded only in the private infrastructure repo, per
the public/private split in `AGENTS.md`. Update it there after each rebuild.

## Target image

| | |
|---|---|
| Schematic | `bootstrap/talos/worker-cm5.schematic.yaml` → `03fea4eca3bc739a71e1f327c381ebad9eb1cfe28b12d574ee670b201b052b32` |
| Installer (in `worker.template.yaml`) | `factory.talos.dev/metal-installer/03fea4eca3bc739a71e1f327c381ebad9eb1cfe28b12d574ee670b201b052b32@sha256:120e60cff87cdd0cd50fceff9c115b375675b622eabc6071f11928b8079dd9ca` (v1.14.4) |
| Flash image | the same schematic's v1.14.4 `metal-arm64.raw.xz`, sha256 `817dedb3c38f26fa8e6177fea6647ab2b9f166534ecb7c7b50883925aedc28a1` (as recorded for zagreus at #193) |

**Pin by digest only.** A Talos 1.14.4 node fails `talosctl upgrade --image repo:tag@sha256:…`
with "installer image … not found in containerd store" after pulling it; the digest-only form
works (measured on zagreus, 2026-10-19). The tag lives in the comment beside the pin.

**Why no extensions.** Nothing in this repo uses iSCSI, and an extension whose service is left
unconfigured blocks boot and reboots the node on a timer. `util-linux-tools` supplied `fstrim`,
which 1.14 replaces with built-in trim (`FilesystemTrimConfig`, see the follow-ups). Rebuilding a
community-build node onto this schematic drops both extensions.

**Why one jump.** Official Talos has no image that boots a CM5 Lite before 1.14.1, so a
community-build node goes from 1.13.2 straight to Factory 1.14.4. That skips Talos's "latest
patch of the current minor first" guidance, which is one reason the path below is a fresh
install rather than an upgrade.

### Verify the pin before using it

The schematic ID is a content hash, so re-posting the schematic must return the same ID:

```bash
curl -fsS -X POST --data-binary @bootstrap/talos/worker-cm5.schematic.yaml \
  https://factory.talos.dev/schematics                     # id must be 03fea4ec…
IMG=factory.talos.dev/metal-installer/03fea4eca3bc739a71e1f327c381ebad9eb1cfe28b12d574ee670b201b052b32:v1.14.4
docker buildx imagetools inspect $IMG | grep Digest        # must equal the pinned digest
```

Check the flash image's sha256 after downloading it. Factory builds images on demand; if either
digest ever differs, stop and find out why before using it.

## The resolver override

Talos 1.14 **applies DHCPv4 search domains** to the node resolver, and the node subnet's DHCP
hands one out. With `ndots:5`, pods then try external names with that suffix first, hit the
zone's public wildcard, and connect to a CDN edge that drops them (AGENTS.md Gotchas #3 and #7).
It was reproduced on the 1.14.1 soak board: GitHub API calls failed TLS.

`worker.template.yaml` therefore carries a `ResolverConfig` with
`searchDomains: {disableDefault: true, domains: []}`. The explicit empty list is load-bearing:
unset means "inherit DHCP" (siderolabs/talos `1c156458a`). A rebuilt node gets it in the config
applied in maintenance mode, so it is in place from the node's first 1.14 boot. The 1.13 nodes
carry it harmlessly: 1.13 ignores DHCP domains.

**Render with the 1.14 client.** A 1.13 talosctl's patcher (`--config-patch`,
`machineconfig patch`, `patch mc`) drops `domains: []`. `bootstrap/mise.toml` pins a 1.14
client, and every apply below checks the rendered file first.

Diagnose on a 1.14 node: `talosctl -n $W get resolvers -o yaml` should show an empty
`searchDomains`. To see where a domain comes from,
`talosctl -n $W get resolverspecs --namespace network-config`: a DHCP one appears as
`dhcp4/<link>/resolvers`, the override as the machine-configuration layer.

## Access

Render the configs with `render-talos` (`bootstrap/README.md` §2). It hydrates every template
into `bootstrap/talos/`, including `talosconfig.yaml` and `worker.yaml`. These carry the full
cluster PKI. They are gitignored, but don't leave them lying around: delete every hydrated
`bootstrap/talos/*.yaml` that isn't a template, schematic or patch when you're done.
`talosctl` comes from `mise` in `bootstrap/`, whose environment loads `bootstrap/athena.zsh`.
That file points `TALOSCONFIG` at the rendered `talos/talosconfig.yaml`; don't override it.

## Checks and the per-node apply

```bash
W=<Talos address>; NODE=<Kubernetes node name>   # match $W to INTERNAL-IP in: kubectl get nodes -o wide
check() {
  talosctl -n $W version                                     # Tag v1.14.4
  talosctl -n $W get extensions                              # only the virtual "schematic" entry, 03fea4ec…
  talosctl -n $W dmesg | grep -E 'mmc0: new|mmc0:.*(error|-110|timeout)' | tail -3
                                                             # "new high speed …" and no errors
  talosctl -n $W get links end0                              # up
  talosctl -n $W get volumestatus EPHEMERAL                  # on the NVMe
  talosctl -n $W get resolvers -o yaml | grep -A3 searchDomains   # empty
  talosctl -n $W get watchdogtimerstatus                     # armed
  kubectl wait --for=condition=Ready node/$NODE --timeout=10m
  kubectl run dnscheck-$RANDOM --rm -i --restart=Never --image=busybox \
    --overrides='{"spec":{"nodeName":"'$NODE'"}}' -- cat /etc/resolv.conf   # no "search" beyond cluster domains
}

# Apply the current worker config to ONE node in maintenance mode. Run from
# bootstrap/talos with worker.yaml rendered. SCRATCH=1 for a scratch worker.
# apply-worker.sh does the same for every node in athena.zsh at once.
apply_one() (    # a subshell, so its traps remove the rendered PKI when it ends
  d=$(mktemp -d) && chmod 700 "$d" || exit 1
  trap 'rm -rf "$d"' EXIT
  # INT/TERM too: zsh may not run a subshell's EXIT trap on Ctrl-C. Exit after
  # cleaning up, so an interrupt never carries on to the apply.
  trap 'rm -rf "$d"; exit 130' INT TERM
  f="$d/node.yaml"
  if [ "${SCRATCH:-0}" = 1 ]; then
    talosctl machineconfig patch worker.yaml --patch @worker-nvme.patch.yaml \
      --patch @watchdog.patch.yaml -o "$f" || exit 1
  else
    talosctl machineconfig patch worker.yaml --patch @watchdog.patch.yaml -o "$f" || exit 1
  fi
  if [ "$(yq -o=json -I=0 'select(.kind == "ResolverConfig") | .searchDomains.domains' "$f")" != "[]" ]; then
    echo "rendered config lost domains: [] (a 1.13 talosctl?); not applying" >&2; exit 1
  fi
  talosctl apply-config --insecure -n $W -f "$f" --dry-run || exit 1
  printf 'apply? [y/N] '; read -r ok     # portable: zsh's read -p means something else
  [ "$ok" = y ] || exit 1
  talosctl apply-config --insecure -n $W -f "$f"
)
```

## Procedure: rebuild from a fresh SD card

This is the path that has worked on these boards. Have the flashed card in hand, and keep the
node's current card, before starting.

1. **Flash** a new SD card with the target flash image, and check its sha256 first. Keep the
   node's current card untouched: it is the rollback.
2. **Drain and shut down:** `kubectl drain $NODE --ignore-daemonsets --delete-emptydir-data`,
   then `talosctl -n $W shutdown`. Swap the card and power on.
3. **Maintenance-mode checks** (`--insecure`): `talosctl -n $W version --insecure` shows
   v1.14.4; `get disks --insecure` shows both the SD card and the NVMe; `dmesg` shows the SD at
   High Speed with no errors.
4. **Clear the NVMe** if it still carries an old layout (an Optane from before the EPHEMERAL move does):
   `talosctl -n $W wipe disk nvme0n1 --insecure`. This is the step that puts EPHEMERAL on the
   NVMe; see `docs/runbook-worker-ephemeral-relocation.md` for what must not be on that disk.
5. **Apply** with `apply_one` (above), reading the dry-run first. Record the node list just
   before it, so the replacement can be spotted the moment it registers:
   `before=$(kubectl get nodes -o name | sort)`.
6. **Join, and cordon at once.** The node comes back with a new machine identity and a new
   Kubernetes node name, and it registers **schedulable**: the old node's drain in step 2 does
   not carry over. Cordon it the moment it appears, before anything else:

   ```bash
   until NEW=$(comm -13 <(printf '%s\n' "$before") <(kubectl get nodes -o name | sort)) && [ -n "$NEW" ]; do sleep 2; done
   kubectl cordon "$NEW"; NODE=${NEW#node/}
   kubectl get pods -A -o wide --field-selector spec.nodeName=$NODE   # only DaemonSet pods expected
   ```

   A short window before the cordon remains. If anything other than a DaemonSet pod landed in
   it, stop and look before going on, and treat a pod with a local-path volume as described
   below. Then delete the old `Node` object. Scratch workers no longer carry a taint
   (`bootstrap/talos/worker-nvme.patch.yaml`), so there is nothing to add by hand. Run `check`.
7. **Prove it reboots, still cordoned:** soak 15 minutes, `talosctl -n $W reboot --wait`, which
   must come back by itself, then `check` again. Then, in the same cordon, the watchdog
   forced-hang test from `docs/runbook-node-watchdogs.md`. If either fails, the node stays
   cordoned.
8. **Only after step 7 passes,** `kubectl uncordon $NODE`. Look at the NIC drop counters
   (`talosctl -n $W get links end0 -o yaml`) before moving to the next node.

**Nothing stateful on an unproven node.** A local-path PersistentVolume lives on one node's disk,
and its PVC pins every future pod of that workload (a StatefulSet replica, say) to that node. A
pod that lands on a replacement node before its reboot test binds its volume there, and if the
node then fails the test, that workload is down with it and cannot be rescheduled elsewhere
without giving up the volume's data. That is why the cordon goes on before anything else and
stays on until step 7 has passed. If such a pod landed anyway, decide with the operator whether
to keep the node up or to delete the PVC and let the workload start fresh elsewhere.

Stop at the first surprise. A single node on the old version is harmless; a second node in an
unknown state is not.

**On a carrier that has not yet run Factory 1.14, prove one node first.** Rebuild no second
node on the same carrier until one has passed step 7's clean reboot. Open questions about
specific nodes live in the private infrastructure repo.

## Upgrading in place

- **Within Factory 1.14.x on this schematic:** `talosctl -n $W upgrade --image <digest-only ref>
  --wait`, then `check`. An A/B upgrade within one overlay was proven on the 1.14.1 soak board,
  and the digest-only form on zagreus at 1.14.4. A new patch release needs its own installer
  digest pinned in `worker.template.yaml` first (re-run "Verify the pin").
- **Across the jump, from the community 1.13.2 build: unproven, don't.** It changes the overlay
  and the minor version on bare metal in one reboot, and the private infrastructure record notes
  this path leaving a board stuck at U-Boot. Rebuild instead.

## Rollback

- **A rebuilt node:** power off, put the old SD card back, power on. The old card still holds the
  node's previous install and its STATE. Untested end to end: expect to delete the new `Node`
  object, and check that the old node rejoins and where its EPHEMERAL is.
- **Within Factory 1.14.x (A/B, same overlay):** `talosctl rollback` flips only the A/B boot
  entry. The EFI partition (U-Boot, DTBs, `config.txt`) is written by the new image's overlay and
  is **not** restored, which is fine while the overlay is unchanged. Proven on the soak board
  (1.14.1 → 1.14.1); rollback still worked after Talos dropped the upgrade fallback entry, about
  4 minutes after a healthy boot.
- **Last resort:** reflash the node's previous image to a fresh card and rejoin it as a new
  worker (`docs/runbook-worker-ephemeral-relocation.md`, "Last resort", including clearing the
  NVMe from maintenance mode first).

## What normal looks like on 1.14 (from the 1.14.1 soak)

- `macb … TX stall detected on queue 0 … re-kicking TSTART`: the upstream recovery patch working,
  with a 1–2 s TX blackout each time. About 10/h under saturating synthetic TX and roughly never
  under real traffic; 0 of 1,439 external probes failed in 4.5 h. Only worry if it comes with
  link resets or NotReady.
- The Ethernet PHY interrupt can't be claimed on the RP1 pinctrl, so the driver polls. Harmless.
- `ethtool` shows EEE "enabled - inactive", because the switch doesn't advertise EEE. A tripwire
  if the switch ever changes; no `dtparam=eee=off` is needed today.
- RX drops appeared in the first hour and did not recur. Don't pre-emptively add `EthernetConfig`
  ring sizes; watch the drop counters instead.
- The fan header: Talos 1.14's mainline kernel has no RP1 PWM driver, so the CM5's
  `cooling_fan` never probes and nothing drives the header (seen on zagreus's carriers). zagreus
  works around it with a GPIO hog in a newer schematic
  (`bootstrap/zagreus/talos/worker-cm5.schematic.yaml`); athena has not taken it. Whether it
  matters depends on the carrier's cooling, which this repo does not record.

## Talos 1.14 follow-ups (not blockers)

- `machine.install`, `nodeLabels`, `nodeTaints`, `kubelet`, `kubePrism` and `hostDNS` are
  deprecated in favour of multi-doc configs (`UnattendedInstallConfig`, `KubeNodeConfig`, …).
  They are still honoured.
- Filesystem trim: configs carried over from 1.13 get **no** `FilesystemTrimConfig` document, so
  periodic trim stays off until one is added. This matters more now that `util-linux-tools` is
  gone.
- Workload isolation (`SecurityProfileConfig`) is off until the document is added. The soak ran
  with it on, including privileged dind, and fresh 1.14 configs turn it on by default.
- `talosctl apply-config --mode=reboot` is gone.
- The control-plane template still names `ghcr.io/siderolabs/installer` and still sets
  `machine.network.disableSearchDomain`, which conflicts with a `ResolverConfig` on 1.14
  (AGENTS.md Gotcha #7). Those nodes need an Image Factory installer and that change before their
  1.14 upgrade.
