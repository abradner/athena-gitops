# Runbook — provisioning the carcinus zone

Standing up the cloud second zone (`docs/design-multi-az-boreas.md` §4c). Ends
with a single-node Talos cluster on free Arm compute, on the mesh VPN, ready
for the GitOps root to be applied.

Everything before §4 needs cloud console or CLI access and is done by the
operator; everything from §4 is ordinary cluster work.

---

## 0. What you are building, and the box it fits in

One instance: **2 OCPU, 12 GB, VM.Standard.A1.Flex**. That is the entire free
Arm allowance — 1,500 OCPU-hours and 9,000 GB-hours a month — so there is no
second instance to be had. Block storage is 200 GB total with a 47 GB minimum
boot volume.

Verified facts this procedure depends on:

- Talos has an `oracle` platform, and the image factory builds `oracle-arm64`.
- Custom image import accepts **QCOW2 or VMDK only**, never raw.
- On Arm shapes, imported images run in **paravirtualized mode only**.
- The primary cluster is entirely arm64, so application images need no changes.

## 1. Build the image

The schematic below is the stock Talos image plus the mesh-VPN system
extension, which the node needs to reach the primary site's database.

    SCHEMATIC=4a0d65c669d46663f377e7161e50cfd570c401f26fd9e7bda34a0216b6f1922b
    VERSION=v1.13.4

    curl -L -o talos-oracle-arm64.raw.xz \
      "https://factory.talos.dev/image/$SCHEMATIC/$VERSION/oracle-arm64.raw.xz"

That schematic id is reproducible: posting the two-line customization
(`systemExtensions.officialExtensions: [siderolabs/tailscale]`) to
`https://factory.talos.dev/schematics` returns the same id.

## 2. Convert to QCOW2

The download is 144 MB compressed and needs roughly **2 GB of free disk** to
decompress and convert. Check first — this has been run on a machine with 6 GB
free and no margin to spare.

    xz -d talos-oracle-arm64.raw.xz
    qemu-img convert -f raw -O qcow2 talos-oracle-arm64.raw talos-oracle-arm64.qcow2
    rm talos-oracle-arm64.raw          # reclaim ~1.3 GB immediately

## 3. Import and launch

In the cloud console:

1. Upload the QCOW2 to an Object Storage bucket.
2. **Compute → Custom Images → Import image.** Source is that object. Operating
   system **Linux**, image type **QCOW2**, launch mode **Paravirtualized** —
   the only mode Arm shapes accept for imported images.
3. Launch an instance from the custom image: shape `VM.Standard.A1.Flex`,
   **2 OCPU / 12 GB**, public IPv4 assigned.
4. Security list: allow inbound **6443** (Kubernetes API) and **50000-50001**
   (Talos API) from your own address only. Nothing else needs to be reachable
   from the internet — public traffic arrives through the tunnel, outbound.

Talos does not use SSH keys, so the instance's key field is irrelevant. It will
boot into maintenance mode and wait for a configuration.

**If instance creation fails with a capacity error**, that is the known
constraint on free Arm compute in busy regions. Retrying, or trying a different
availability domain in the region, is the remedy. It is not a fault in this
procedure.

## 4. Apply the machine configuration

Carcinus is a single schedulable control plane, so it needs its own rendered
config — see `bootstrap/README.md` for how configs are rendered, and note the
cluster-identity fields differ from the primary's.

    talosctl apply-config --insecure --nodes <public-ip> --file carcinus-controlplane.yaml
    talosctl bootstrap --nodes <public-ip>
    talosctl kubeconfig --nodes <public-ip>

Two differences from the primary site's nodes, both consequences of the
platform rather than choices:

- `machine.install.disk` is the paravirtualized boot volume, not the primary's
  device path. Confirm with `talosctl get disks` from maintenance mode rather
  than assuming.
- The platform supplies networking, so no static addressing is configured.

## 5. Join the mesh and verify

The mesh extension needs an auth key supplied through machine config. Once the
node is on the mesh, before going further, confirm the thing the whole zone
depends on:

    # from the node, that the primary's database is reachable
    nc -zv postgres.infra.<primary-zone> 5432

If that fails, nothing downstream is worth attempting — the standby cannot
stream and the zone is decorative.

## 6. Hand over to GitOps

From here it is ordinary cluster work: install the CNI and Argo CD as
`bootstrap/kubernetes/provision.sh` does, inject the secrets-store token, and
apply the carcinus root application.

## Keeping the instance

Free-tier instances may be reclaimed when 95th-percentile CPU, network **and**
memory all sit under 20% across seven days. All three must be low, so a warm
standby with a real working set is not a candidate. Worth an alert on the
memory figure drifting toward the line, because the failure mode is silent and
the remedy after the fact is rebuilding the zone.
