# Investigation: Upstream Talos 1.14.1 on CM5 Lite from SD (2026-09)

Can athena's CM5 Lite workers leave the community Talos builds and run stock Talos from Image
Factory? This records a test on a spare CM5 Lite that boots from SD like the fleet: a throwaway
single-node cluster, athena-like workloads, a GitHub Actions runner soak, reboots, and a Factory
upgrade and rollback. It supersedes the "CM5 Lite image status" section of
[`investigation-2026-09-worker-storage.md`](investigation-2026-09-worker-storage.md).

Topology (addresses, the board, the exact schematic ID, the switch) is in the private
infrastructure repo, per the public/private split in `AGENTS.md`.

> **Status:** three kinds of claim appear below, and each is labelled where it's made. Results
> **measured** on the test board (the tables under "What was tested", the EFI inventory, the
> Ethernet counters). Facts **read from upstream**: the Image Factory API, Talos and
> sbc-raspberrypi source, commits, issues and release notes. **Inferences**, marked as inferred
> or *untested*. The upgrade from the fleet's exact community image, which decides the rollback
> story, has **not** been tested (see "Next steps"). Until it is, a reflash is the rollback.

## Summary

- **Upstream v1.14.1 boots a CM5 Lite from SD.** SD came up (UHS-I SDR104) on every one of
  ten boots, including a hard power cycle, with zero mmc errors. `siderolabs/sbc-raspberrypi#98`
  reports CM5 Lite SD failing with -110 on earlier device trees.
- **The image is plain Image Factory:** overlay `siderolabs/sbc-raspberrypi` `rpi_5`, no
  customisation. Extensions are orthogonal. athena's production schematic will differ (see
  "Schematic" below).
- **Treat v1.14.1 as the floor; there is no known-good Factory 1.13.x hop.** Every Factory 1.13.x
  release ships overlay v0.2.0, which predates the device-tree regeneration and is the era #98
  describes. v1.14.0 ships v0.2.1, which broke CM5 Ethernet. v1.14.1 (overlay v0.2.2) is the only
  release shown here to work. The fleet therefore goes from the community 1.13.2 build
  **straight to** Factory 1.14.1. That skips Talos's "latest patch of each minor first" guidance,
  and is *untested*.
- **Ethernet has one known wart, and it is survivable:** the macb TX-ring stall
  (`sbc-raspberrypi#91`) still happens: roughly 10–12/h in one ~4 h run of saturating synthetic
  TX, near zero under runner traffic with cached images. The upstream recovery patch clears
  each one in about 1–2 s. Nothing escalated to an outage in 4.5 h of soak.
- **EEE is not in play on athena's switch.** EEE negotiates `enabled - inactive` because the switch
  doesn't advertise it, so the EEE wake race from #91 cannot fire. It becomes relevant if the
  switch or its port config changes.
- **No kernel patches needed so far.** Every issue found has a config-level answer or needs none.

## What was tested

Single node, control plane with scheduling allowed, config generated fresh by a 1.14.1 client
in the 1.14 multi-document form. Settings mirror the worker template where they apply: KubePrism
on 7445, hostDNS forwarding kube DNS to the host, XFS project quotas, kubelet image GC 75/55,
CNI none, kube-proxy disabled. Cilium 1.19.4 with `bootstrap/kubernetes/cilium-values.yaml`,
Gateway API v1.2.1. Kubernetes 1.36.1 to match the fleet.

| Area | Result |
|---|---|
| SD across 10 boots (maintenance-mode boot, 6 reboots, upgrade, rollback, power cycle) | SDR104 every time, 0 mmc / -110 / I/O errors |
| local-path-provisioner (as vendored, `/var/local-path-provisioner`) | PVC bound; 512 MiB blob sha256-verified after every boot |
| emptyDir `sizeLimit` breach | Evicted: "Usage of EmptyDir volume exceeds the limit"; `/var` is XFS with `prjquota` |
| Image GC 75/55 | Triggered at 78 %, removed unused images (56 → 36) |
| ARC runner scale set (dind, ephemeral) | 5 soak runs green, plus 1 cancelled after a test-harness fault (not a job failure): container build, Go toolchain compile, Linux clone, 3 GiB fsync'd write with O_DIRECT verify; runners deregister after each job |
| 6 × `talosctl reboot` | All clean: Ready, every Deployment/DaemonSet available, fresh PVC checksum |
| `talosctl upgrade -i <factory metal-installer>` (same version) | Slot A → B, all checks green |
| `talosctl rollback` | Back to slot A, all checks green |
| Hard power cycle | Offline about 84 s; SD, Ethernet, Ready, all workloads and the PVC checksum back within about 5 min |

Runner timings on the CM5 Lite, EPHEMERAL on SD: dind build compiling ripgrep 2m30s, Go
`make.bash` 5m06s, 3 GiB fsync'd SD write about 28 MB/s.

## Findings that change how athena is operated

### 1. There is no gentle upgrade path, only a jump

Factory's `rpi_5` overlay by Talos release:

| Talos | Overlay | CM5 Lite from SD | Source |
|---|---|---|---|
| v1.13.0 – v1.13.10 | v0.2.0 | Expected to fail SD | #98 (CM5 Lite SD -110 from a fixed 1.8 V regulator in the device tree); not tested here |
| v1.14.0 | v0.2.1 | Ethernet broken | `sbc-raspberrypi` `b1fe7bda` ("restore CM5 Ethernet initialization", released in v0.2.2); not tested here |
| v1.14.1 | v0.2.2 | SD and Ethernet work | Measured here |

The overlay-by-release mapping comes from the Image Factory API. The board still logs #98's
`cannot verify signal voltage switch` warning and then enumerates at SDR104 anyway. #98 is still
open. The most likely fix is v0.2.1's regeneration of the Pi 5 device trees from the kernel Talos
actually ships (`3efdf1a0`), but that's inferred, not bisected. Either way, the only Factory target
shown to work is v1.14.1. The fleet would have to reach it directly from the community build, and
that jump is *untested*: the only upgrade performed here was Factory v1.14.1 to itself.

### 2. Rollback does not restore the EFI partition, and on a Pi that holds the device tree

The boot chain is: Pi firmware → `config.txt` → `u-boot.bin` → `EFI/boot/BOOTAA64.efi` (GRUB) →
`/A` or `/B` kernel on BOOT. The firmware picks the board's DTB **from the EFI partition**,
outside the A/B scheme. Measured across a same-version upgrade: the upgrade rewrote GRUB and
left the other 400 files (u-boot, every DTB, `config.txt`, overlays) byte-identical (same
overlay version). Rollback left EFI untouched.

Consequence: after the community → Factory jump, `talosctl rollback` would boot the community
1.13.2 kernel with Factory's overlay v0.2.2 u-boot, DTBs and `config.txt`. Whether that boots,
with SD and Ethernet, is *untested*. Until it is known, treat rollback from Factory 1.14.1 as
**unproven**, and keep a reflash (SD image) as the real rollback.

### 3. `talosctl upgrade` without `-i` installs the wrong image

A 1.14 client's default installer is the *vanilla* Factory schematic, with no Raspberry Pi
overlay. Always pass `-i factory.talos.dev/metal-installer/<cm5 schematic>:<version>`.

### 4. Talos 1.14 starts applying DHCP search domains, which brings back Gotcha #3

AGENTS.md Gotcha #3 reproduced on this board. DHCP on the node subnet hands out a search
domain, pods use `ndots:5`, and a lookup of an external hostname went through the zone's
wildcard record. The failure showed up as a TLS handshake failure from the runner controller.

The cause is a behaviour change, not this board's lease: the v1.14.0 release notes say "DHCPv4
search domains are now applied to the resolver configuration" (`siderolabs/talos@5b81b20d3`).
1.13 ignored them, which is why the existing 1.13 workers show none on the same DHCP. **Every
worker will pick the domain up on its upgrade to 1.14** unless the override below is in place
first. The general rule belongs in AGENTS.md's Gotchas, and the CM5 worker upgrade runbook
carries it.

In the 1.14 `ResolverConfig`:
- `searchDomains.disableDefault: true` is the analogue of `machine.network.disableSearchDomain`.
  It only stops search domains **derived from the hostname FQDN**.
- Search domains supplied **by DHCP** need `searchDomains.domains: []`.

On this board, `talosctl get resolverspecs --namespace network-config` shows the domain on the
`dhcp4/<link>/resolvers` spec (layer `operator`). DHCP supplied no hostname, so nothing was derived
from an FQDN. That's a different mechanism from Gotcha #3's control-plane case, and Talos's
resolver controller (`resolver_config.go`) applies `disableSearchDomain` only to the
hostname-derived domain. The explicit empty list is what overrides DHCP; leaving the field unset
inherits it (`siderolabs/talos@1c156458a`).

Set both fields before or with each node's upgrade, and verify from inside a pod
(`cat /etc/resolv.conf`), not from the node. A DHCP-server-side fix (not handing that domain to
the node subnet) covers every Talos version at once.

### 5. The metal image doesn't run the installer on first boot

Applying config to a board booted from the Factory SD image logs `install sequence: 0
phase(s)`. The SD is already an install. STATE and EPHEMERAL are created in place, and
EPHEMERAL grows to the rest of the card. The installer image is first exercised by the first
`talosctl upgrade`, so a node can look healthy with a wrong installer reference in its config.

## Ethernet detail

- **macb TX stall** (`TX stall detected on queue 0 … re-kicking TSTART`). The detection-and-kick
  patch is upstream (in `siderolabs/pkgs` since about 1.13.1). The five stalls checked against a
  5 Hz external ping each lined up with a 1.2–2.2 s gap. Nothing escalated: none of 1,439
  ten-second TCP probes of the Talos API failed over 4.5 h. Stalls clustered at bursty,
  connection-start traffic (image pulls) more than steady streams.
- **EEE** is advertised by the PHY but not by athena's switch, so it negotiates
  `enabled - inactive`. The EEE fix from #91 (`raspberrypi/linux#7270`) is vendor-kernel-only and
  Sidero won't carry it. That's fine while the switch doesn't do EEE; it's a tripwire if the
  switch changes.
- **RX ring drops.** About 9.9k `rx_resource_errors` accumulated at the default 512-entry rings, all
  in the first hour. That hour also had the initial image pulls, a multi-GB image-GC test, and a
  control plane crashlooping on etcd-on-SD. None could be reproduced afterwards: not with a steady
  919 Mbit/s RX stream, not with RX while the CPU was saturated, not in a real runner soak run
  (9 overruns, 0 errors), and not with a parallel pull of three large images (0/0). `EthernetConfig`
  `rings: {rx: 4096, tx: 4096}` applies live without dropping the link, but its effect couldn't be
  measured without a reproducible baseline. Don't add it pre-emptively; watch the NIC drop counters
  on the fleet.
- The PHY's IRQ can't be claimed on the RP1 pin controller, so it polls. This is harmless in
  practice.

## Schematic

The test used the bare `rpi_5` overlay with no extensions. athena's workers currently carry
`iscsi-tools`, and whether to keep it is an open question in the private tracker. The
production schematic is whatever that decision yields: generate it deliberately and pin the ID
and overlay version in the private repo. Extensions don't touch SD or Ethernet, but a different
schematic is a different image, so smoke-test the exact one before the fleet moves.

## Not athena-relevant, recorded anyway

- The PWM fan driver never binds (`pwm-fan: Could not get PWM`), so a carrier fan isn't
  controlled. athena's nodes use passive heatsinks and an external blower.
- etcd on the SD card is slow enough (0.3–1.4 s reads under image pulls) that the test's
  scheduler and controller-manager lost their leader leases until the lease timeouts were
  lengthened. athena's control plane is on VMs; workers don't run etcd.

## Next steps

1. **The decisive test.** Reflash the spare board with the fleet's exact community v1.13.2 image,
   join it, `talosctl upgrade -i <factory installer>:v1.14.1`, verify, then `talosctl rollback` and
   verify. That proves (or disproves) the direct jump and the cross-boundary rollback in one go.
2. **Boot-test the production schematic** (with whatever extensions the private tracker settles
   on) on the spare before any worker moves.
3. **Then one worker at a time,** starting with a scratch worker. After each: SD enumeration in
   `dmesg` with no -110, `end0` up, the boot slot flipped, Ready with a fresh heartbeat, and NIC
   drop counters. Only then the next.
4. **Keep a flashed community SD card on hand** as the rollback until step 1 says otherwise.

The installer pin and the step-by-step procedure belong in the CM5 worker upgrade runbook.
