# Runbook: Upgrading the CM5 Lite Talos Workers

How the metal workers get a new Talos version. The control plane is out of scope. Its
nodes are generic arm64 VMs and upgrade from Image Factory like any other Talos node.

**Every live upgrade or rollback needs the operator's explicit go-ahead, every time.**
Never run an upgrade in the same maintenance window as a storage or EPHEMERAL change. Two changes
in one reboot means you cannot tell which one broke the node.

## Why the workers are special

The workers are **Raspberry Pi Compute Module 5 Lite** boards that boot from SD (`mmcblk0`).
What boots them, by Talos version (the overlay is bound per Talos release by Image Factory):

| Talos (official, Image Factory `rpi_5`) | Overlay | CM5 Lite | Basis |
|---|---|---|---|
| 1.13.0 – 1.13.10 | sbc-raspberrypi v0.2.0 | SD init fails (-110) | upstream report, siderolabs/sbc-raspberrypi#98. Not tested here. |
| 1.14.0 | v0.2.1 | No Ethernet. Never a valid target. | upstream commit `b1fe7bda` ("restore CM5 Ethernet initialization", in v0.2.2). Not tested here. |
| 1.14.1 | v0.2.2 | **Works** | **tested:** soaked on a spare CM5 Lite from SD, 2026-09-25 |

Overlay versions per Talos release come from the Factory API. Why 1.14.1 boots SD is inferred,
not proven: probably v0.2.1's DTB regeneration from the Talos kernel (`3efdf1a0`). #98 is still
open, and the soak board still logs its "cannot verify signal voltage switch" warning before
enumerating at SDR104. The soak's full write-up is in the CM5 upstream-Talos investigation
([#145](https://github.com/abradner/athena-gitops/pull/145),
`docs/investigation-2026-09-cm5-upstream-talos.md`).

Two installer defaults are wrong for these boards, so **always pass `--image`**:

- A 1.13 `talosctl upgrade` defaults to `ghcr.io/siderolabs/installer`, which has no board
  overlay and is not published from 1.14.
- A 1.14 `talosctl upgrade` defaults to the vanilla Image Factory schematic, which also has no
  Pi overlay.

## Where the fleet is now

Established 2026-09-24. Recorded so nobody re-derives it.

The fleet was flashed from a community build, **`johnlaur/talos-builder` v1.13.2** (Raspberry Pi
vendor kernel, `iscsi-tools` + `util-linux-tools`). The asset is `metal-arm64-rpi5.raw.xz`,
sha256 `8d2d2dad3eafab3d8779520fa0669f0b0f48d7d616d1af4c1958d1bdccb0d27b`. It was matched by the
asset digest, by Talos SHA `23154840` in the image's `init`, and by the kernel banner
`6.18.29-talos … #1 SMP Sun May 17 19:09:54 UTC 2026`. That source has published nothing since,
and its installer image cannot be pulled.

**Keep a copy of that image.** It is the recovery path (see Rollback).

## Target image

| | |
|---|---|
| Installer | `factory.talos.dev/metal-installer/a636242df247ad4aad2e36d1026d8d4727b716a3061749bd7b19651e548f65e4:v1.14.1@sha256:fc4c62ec958a6e93823154a75dbb5fadfe7741a06fd15034569528a59450d2e7` |
| Schematic | `overlay: {image: siderolabs/sbc-raspberrypi, name: rpi_5}`, `customization: {}`. No extensions, no extra kernel args. |
| Talos / kernel | v1.14.1 (`2f86b9d2`), 6.18.51-talos |

**Why no extensions.** This is the exact schematic that was soaked on hardware. The fleet's
`iscsi-tools` is unused: nothing in this repo uses iSCSI, and it conflicts with 1.14 workload
isolation. `util-linux-tools` supplied `fstrim`, which 1.14 replaces with built-in trim
(`FilesystemTrimConfig`, see the follow-ups). The fleet-equivalent schematic with both
extensions (`b00ac840…`) has never been booted. If they turn out to be needed, that is a new
schematic, and it gets its own soak.

**Why one jump.** Official Talos has no image that boots a CM5 Lite before 1.14.1, so the fleet
goes community 1.13.2 → Factory 1.14.1 directly. That skips Talos's "latest patch of the current
minor first" guidance. The only 1.13-latest hop would be another community build, which trades
one unmaintained dependency for another. (One such candidate, `yama6a/talos-raspberry-pi5`, was
the plan before upstream caught up. It is well-attested and remains the fallback if a future
official release regresses CM5.)

### Verify the pin before using it

The schematic ID is a content hash, so re-posting the schematic must return the same ID:

```bash
printf 'overlay:\n  image: siderolabs/sbc-raspberrypi\n  name: rpi_5\ncustomization: {}\n' \
  | curl -fsS -X POST --data-binary @- https://factory.talos.dev/schematics   # id must be a636242d…
IMG=factory.talos.dev/metal-installer/a636242df247ad4aad2e36d1026d8d4727b716a3061749bd7b19651e548f65e4:v1.14.1
docker buildx imagetools inspect $IMG | grep Digest    # must equal the pinned digest
c=$(docker create --platform linux/arm64 $IMG)
docker export $c | tar -t | grep -E 'cm5l|u-boot.bin|vmlinuz.efi'   # CM5L DTBs, U-Boot, UKI
docker rm $c
```

Factory builds installers on demand. The digest was identical on 2026-09-25 and 2026-09-26. If
it ever differs, stop and find out why before using it.

## Before the first worker: the resolver fix

Talos 1.14 starts **applying DHCPv4 search domains** to the node resolver. The node subnet's DHCP
hands one out. With `ndots:5`, pods then try external names with that suffix first, hit the
zone's public wildcard, and connect to a CDN edge that drops them. This is AGENTS.md Gotcha #3,
which until now only affected the control plane. It was reproduced on the soak board: GitHub API
calls failed TLS.

`worker.template.yaml` therefore carries a `ResolverConfig` with
`searchDomains: {disableDefault: true, domains: []}`. The explicit empty list is load-bearing:
unset means "inherit DHCP" (siderolabs/talos `1c156458a`, in 1.14 only). Both 1.13 and 1.14
accept the document, and 1.13 ignores DHCP domains anyway, so **apply it to each worker before
its upgrade**, while it is still on 1.13. Then the first 1.14 boot already has it, and pods
created by that boot get a clean `resolv.conf`.

- **How to apply it:** `stage` in [Checks and staging](#checks-and-staging), per node. Not
  `apply-worker.sh`: that script uses `--insecure`, so it only reaches nodes in maintenance mode.
  Scratch workers need the scheduling patch merged in, and the **1.13** talosctl pinned in
  `bootstrap/mise.toml` drops `domains: []` when it patches (`--config-patch`,
  `machineconfig patch`). The 1.14.1 client keeps it, so `stage` patches with 1.14.1 and refuses
  a file that has lost the list. An unpatched `apply-config` stores the bytes as given.
- **Diff first** (AGENTS.md Gotcha #4). `stage` shows the server's own `--dry-run` diff and waits.
  Expect **exactly two** changes against a node that matches the old template: the added
  `ResolverConfig`, and `machine.install.image` moving to the Factory pin. The image change is
  harmless because nothing reads it except an upgrade run without `--image`. Anything else
  means live and git have drifted: stop and reconcile first.
- **If a 1.13.2 node rejects the document**, upgrade it anyway, then apply it straight after the
  1.14 boot and **reboot the node once more** before `check` and uncordoning. Pods keep the
  `resolv.conf` they were created with, so DaemonSet pods started by the upgrade boot would keep
  the DHCP domain, while a fresh test pod would look clean.
- **Diagnose on the 1.14 node:** `talosctl -n $W get resolvers -o yaml` should show an empty
  `searchDomains`. To see where a domain comes from, check
  `talosctl -n $W get resolverspecs --namespace network-config`: a DHCP one appears as
  `dhcp4/<link>/resolvers`, and the override as the machine-configuration layer.

## Access

Render the configs with `render-talos` (`bootstrap/README.md` §2). It hydrates every template
into `bootstrap/talos/`, including `talosconfig.yaml` and `worker.yaml`. These carry the full
cluster PKI. They are gitignored, but don't leave them lying around: delete every hydrated
`bootstrap/talos/*.yaml` that isn't a `.template.yaml` when you're done. `talosctl` comes from
`mise` in `bootstrap/`, whose environment loads `bootstrap/athena.zsh`. That file already points
`TALOSCONFIG` at the rendered `talos/talosconfig.yaml`, so don't override it. The 1.14.1 client for patching runs with `mise exec talosctl@1.14.1 -- talosctl`.
The worker addresses are in `bootstrap/athena.zsh`.

## Checks and staging

```bash
W=<Talos address>; NODE=<Kubernetes node name>   # match $W to INTERNAL-IP in: kubectl get nodes -o wide
check() {
  talosctl -n $W version                                     # Tag + SHA
  talosctl -n $W get extensions                              # Factory: only the virtual "schematic" entry, version a636242d…
  talosctl -n $W dmesg | grep -E 'mmc0: new|mmc0:.*(error|-110|timeout)' | tail -3
                                                             # "new … SDR104" and no errors
  talosctl -n $W get links end0                              # up
  talosctl -n $W get resolvers -o yaml | grep -A3 searchDomains   # on 1.14: empty
  talosctl -n $W read /proc/cmdline | grep -o 'BOOT_IMAGE=[^ ]*'   # which A/B slot booted
  kubectl wait --for=condition=Ready node/$NODE --timeout=10m
  kubectl run dnscheck-$RANDOM --rm -i --restart=Never --image=busybox \
    --overrides='{"spec":{"nodeName":"'$NODE'"}}' -- cat /etc/resolv.conf   # no "search" beyond cluster domains
}

# Put the current worker template on one node. Run from bootstrap/talos, with worker.yaml rendered.
# SCRATCH=1 for a scratch worker: it merges worker-nvme.patch.yaml with the 1.14.1 client.
stage() {
  local f=worker.yaml rc=0
  if [ "${SCRATCH:-0}" = 1 ]; then
    f=$(mktemp) && chmod 600 "$f"
    mise exec talosctl@1.14.1 -- talosctl machineconfig patch worker.yaml \
      --patch @worker-nvme.patch.yaml -o "$f" || rc=1
  fi
  if [ $rc = 0 ] && [ "$(mise exec yq@4 -- yq -o=json 'select(.kind == "ResolverConfig").searchDomains.domains' "$f")" != "[]" ]; then
    echo "ResolverConfig lost domains: [] in $f; not applying" >&2; rc=1
  fi
  if [ $rc = 0 ]; then
    if talosctl -n $W apply-config -f "$f" --dry-run; then   # read it: exactly two changes (see above)
      printf 'apply? [y/N] '; read -r ok     # portable: zsh's read -p means something else
      [ "$ok" = y ] && talosctl -n $W apply-config -f "$f" || rc=1
    else
      echo "dry-run failed; not applying" >&2; rc=1
    fi
  fi
  [ "$f" = worker.yaml ] || rm -f "$f"
  return $rc
}
```

`check` runs after every boot. `stage` runs once per node, before its upgrade.

## Procedure

### 0. Prove the jump on the spare board first

The soak proved Factory 1.14.1 on a CM5 Lite, including an A/B upgrade and rollback. It did
**not** prove the jump *from* the community build, which is the step every worker takes. So
prove it once, on the spare CM5 Lite, never on a fleet worker. The spare must be the same CM5
Lite and carrier board as the fleet: the overlay ships only the official-carrier CM5 Lite DTBs
(`cm5l-cm4io`, `cm5l-cm5io`), so a different carrier isn't covered by the soak.

Step 0 also proves two things nothing else has:
- a 1.13 `machined` pulling a `tag@digest` installer ref;
- whether rollback across the jump boots.

1. Flash the spare's SD with the exact johnlaur v1.13.2 image (sha256 above), and join it as
   a worker with the **pre-#144** config, so it looks like a fleet node. Derive that from the
   rendered `worker.yaml`: #144 only added the `ResolverConfig` document and changed the install
   image, so removing and reverting those gives exactly the old template, hydrated. (Checked
   structurally against the template at `64491f9`.)
   ```bash
   f=$(mktemp) && chmod 600 $f
   mise exec yq@4 -- yq 'select(.kind != "ResolverConfig") | (select(.machine) | .machine.install.image) = "ghcr.io/siderolabs/installer:v1.13.4"' worker.yaml > $f
   talosctl apply-config --insecure -n $W -f $f     # the spare is in maintenance mode
   rm -f $f
   ```
   Then `check`.
2. `stage` the current template, then `check`.
3. `talosctl -n $W upgrade --image <pin> --wait`, then `check`. Expect v1.14.1 / `2f86b9d2`, only
   the `schematic` extension, the other boot slot, and an empty `searchDomains`.
4. `talosctl -n $W reboot --wait`, and `check`. Then power-cycle it hard, and `check` again.
5. Try `talosctl -n $W rollback`, then `check`. **The outcome is unknown, and that is the point
   of trying it here.** See Rollback. Record what happens either way. If it doesn't come back,
   reflash (see Rollback) and write the result into this runbook.

### 1. Roll out one worker at a time

Start with a scratch worker. For each worker, only after the previous one is healthy:

```bash
SCRATCH=<0|1> stage                # while still on 1.13; then confirm the apply
kubectl drain $NODE --ignore-daemonsets --delete-emptydir-data
talosctl -n $W upgrade --image <pin> --wait
check
kubectl uncordon $NODE
```

(`talosctl upgrade` cordons and drains on its own too. The explicit drain surfaces PDB blocks
before the reboot, not during it.)

Stop at the first surprise. A single node on the old version is harmless. A second node in an
unknown state is not. After each node, look at the NIC drop counters (`talosctl -n $W get
links end0 -o yaml`) before moving on.

### 2. After the fleet is done

Move `worker.template.yaml`'s pin comment from "the fleet has not taken it yet" to the new
state, in a PR. Then work through the 1.14 follow-ups below.

## Rollback

`talosctl rollback` flips only the A/B boot entry. The EFI partition (U-Boot, DTBs,
`config.txt`) is written by the new image's overlay and is **not** restored.

- **Within Factory 1.14.1 (A/B, same overlay): proven.** The soak upgraded 1.14.1 → 1.14.1 and
  rolled back. The upgrade changed only `EFI/boot/BOOTAA64.efi`. All ~400 overlay files were
  byte-identical, and rollback left EFI untouched. Rollback still worked after Talos dropped the
  upgrade fallback entry, about 4 minutes after a healthy boot.
- **Across the jump (back to community 1.13.2): unproven.** It would boot the vendor kernel
  against DTBs generated from the mainline kernel, and whether SD and Ethernet come up is
  unknown until step 0 answers it. Until then, **the rollback across the jump is a reflash**:
  write the johnlaur v1.13.2 image to a fresh SD, and rejoin the node as a fresh worker. That
  is the "Last resort" in `docs/runbook-worker-ephemeral-relocation.md`, including clearing the
  NVMe first from maintenance mode. Have a flashed SD in hand before starting the rollout.

## What normal looks like on 1.14.1 (from the soak)

- `macb … TX stall detected on queue 0 … re-kicking TSTART`: this is the upstream recovery patch
  working, with a 1–2 s TX blackout each time. It happened about 10/h under saturating
  synthetic TX and roughly never under real traffic, and 0 of 1,439 external probes failed in
  4.5 h. Only worry if it comes with link resets or NotReady.
- The Ethernet PHY interrupt can't be claimed on the RP1 pinctrl, so the driver polls. Harmless.
- `ethtool` shows EEE "enabled - inactive", because the fleet's switch doesn't advertise EEE.
  That's a tripwire if the switch ever changes. No `dtparam=eee=off` is needed today.
- RX drops appeared in the first hour and did not recur. Don't pre-emptively add
  `EthernetConfig` ring sizes; watch the drop counters instead.
- Upgrade time, from command to workloads back: about 7 minutes. A hard power cycle is about
  84 s offline, then about 5 minutes to workloads.

## Talos 1.14 follow-ups (not blockers)

An upgraded node keeps its existing v1alpha1 config and behaves as before. These are opt-in:

- `machine.install`, `nodeLabels`, `nodeTaints`, `kubelet`, `kubePrism` and `hostDNS` are
  deprecated in favour of multi-doc configs (`UnattendedInstallConfig`, `KubeNodeConfig`, …).
  They are still honoured.
- Filesystem trim: upgraded clusters get **no** `FilesystemTrimConfig` document, so periodic
  trim stays off until one is added. This matters more now that `util-linux-tools` is gone.
- Workload isolation (`SecurityProfileConfig`) is off on upgraded clusters until the document is
  added. The soak ran with it on, including privileged dind, and fresh 1.14 configs turn it on
  by default.
- `talosctl apply-config --mode=reboot` is gone.
- The control-plane templates still name `ghcr.io/siderolabs/installer`. Those nodes need an
  Image Factory installer, and their own resolver check, before their 1.14 upgrade. That's
  tracked privately.
