# Runbook — provisioning the deimos zone

Standing up the cloud second zone (`docs/design-multi-az-boreas.md` §4c). Ends
with a single-node Talos cluster on a cloud VM, on the mesh VPN, ready for the
GitOps root to be applied.

Everything before §5 needs cloud console or CLI access and is done by the
operator; everything from §5 is ordinary cluster work.

---

## 0. The shape, and why

One instance, **2 vCPU / 8 GB, Arm (Graviton)**, in the Sydney region.

The size is not a guess. The primary cluster's `kube-apiserver` settles at
about 1.4 GB, which is why its control-plane nodes were raised to 4 GB. This
node carries a control plane *and* the workloads *and* the database standby and
object-storage node, so 4 GB would leave roughly 1.5 GB for everything after
Kubernetes. 8 GB is the smallest size that is not an act of optimism.

Arm because the images are multi-arch — the publish workflow assembles
per-architecture digests into a manifest list — and Arm instances are cheaper
per GB. amd64 would work equally well if Arm capacity is unavailable.

Sydney because a failover serves the same visitors as the primary site, and the
round trip is paid on every dynamic request. Static assets stay edge-cached
regardless.

## 1. Before anything else: a budget

The account runs on promotional credits, and **access to services ends when
they are exhausted or when the promotional period ends, whichever is first**.
There is no bill to warn you; the account simply stops.

So set a cost budget with alerting *before* launching anything. It is also
worth credits as one of the onboarding activities, which makes it free to do
the right thing. Set the alert well below the remaining balance — the goal is
to learn about a runaway before it eats the zone, not after.

Sanity-check the burn rate against the balance before committing to an instance
size, and remember storage and data transfer are charged separately from
compute.

## 2. Get the Talos image

    SCHEMATIC=4a0d65c669d46663f377e7161e50cfd570c401f26fd9e7bda34a0216b6f1922b
    VERSION=v1.13.4

    curl -L -o talos-aws-arm64.raw.xz \
      "https://factory.talos.dev/image/$SCHEMATIC/$VERSION/aws-arm64.raw.xz"
    xz -d talos-aws-arm64.raw.xz

That schematic is stock Talos plus the mesh-VPN system extension, which the
node needs to reach the primary site's database. It is reproducible: posting
the two-line customization (`systemExtensions.officialExtensions:
[siderolabs/tailscale]`) to `https://factory.talos.dev/schematics` returns the
same id.

Substitute `aws-amd64` if you end up on an x86 instance type.

## 3. Turn the image into an AMI

Upload the raw image to S3, then import it as a snapshot and register that
snapshot as an AMI. The import needs a `vmimport` service role with access to
the bucket — that role does not exist by default and its absence is the usual
first failure.

Register the AMI with **ENA support enabled**, boot mode **UEFI** for Arm, and
the architecture matching the image. Getting boot mode wrong produces an
instance that launches and never becomes reachable, with no console clue.

## 4. Launch

Instance type `t4g.large`, the AMI from §3, a public IP, and a root volume of
at least 20 GB.

Security group, inbound, **from your own address only**:

| Port | Purpose |
|---|---|
| 6443 | Kubernetes API |
| 50000-50001 | Talos API |

Nothing else needs to be reachable from the internet. Public traffic arrives
through the tunnel, which dials outbound.

Talos does not use SSH, so the key pair field is irrelevant — leave it empty
rather than hunting for a key. The instance boots into maintenance mode and
waits for a configuration.

## 5. Apply the machine configuration

Deimos is a single schedulable control plane, so it needs its own rendered
config — see `bootstrap/README.md` for how configs are rendered, and note that
the cluster-identity fields differ from the primary's.

    talosctl apply-config --insecure --nodes <public-ip> --file deimos-controlplane.yaml
    talosctl bootstrap --nodes <public-ip>
    talosctl kubeconfig --nodes <public-ip>

Two differences from the primary site's nodes, both consequences of the
platform rather than choices:

- The install disk is the cloud root volume, not the primary's device path.
  Confirm with `talosctl get disks` from maintenance mode rather than assuming.
- The platform supplies networking, so no static addressing is configured.

## 6. Join the mesh and verify the one thing that matters

The mesh extension takes an auth key through machine config. Once the node is
on the mesh, confirm what the whole zone depends on before going further:

    # from the node, that the primary's database is reachable
    nc -zv postgres.infra.<primary-zone> 5432

If that fails, nothing downstream is worth attempting — the standby cannot
stream and the zone is decorative.

## 7. Hand over to GitOps

From here it is ordinary cluster work: install the CNI and Argo CD as
`bootstrap/kubernetes/provision.sh` does, inject the secrets-store token, and
apply the deimos root application.

## Living within the credits

Compute is the dominant cost and scales with instance size, so the lever is the
instance type. Sizing for the event and shrinking afterwards is a stop, change
type, start — the root volume and its data survive.

The harder constraint is the end date. When credits or the promotional period
run out, **access ends**, so this zone is a bridge rather than a home. Whatever
replaces it — the other house returning, or a different provider — should be
decided well before that date rather than discovered on it.
