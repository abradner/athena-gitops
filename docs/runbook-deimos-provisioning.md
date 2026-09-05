# Runbook — provisioning the deimos zone

Standing up the cloud second zone (`docs/design-multi-az-boreas.md` §4c). Ends
with a single-node Talos cluster reaching the primary site's data tier over the
mesh, with **no inbound rules from the internet at all**.

Placeholders throughout — `<elastic-ip>`, `<private-ip>`, `<tailnet-ip>`,
`<primary-zone>`, `<internal-resolver>`. Concrete addresses, account
identifiers and the exact command log live in the private infrastructure repo.

Everything before §7 needs cloud credentials; everything after is ordinary
cluster work.

---

## 0. What decides the shape

**The account may only launch free-tier-eligible instance types**, credits or
not — anything else is refused outright. Ask what is permitted rather than
assuming credits buy any shape, because exactly one eligible type has 8 GB and
it is x86_64. The Arm options stop at 2 GB.

That architecture switch costs nothing: the application images are multi-arch.

8 GB is a floor rather than a preference. The primary cluster's
`kube-apiserver` settles near 1.4 GB, which is what forced its control-plane
nodes to 4 GB, and this node additionally carries the workloads, a database
standby and an object-storage node.

## 1. A budget, before any instance

The account runs on promotional credits, and **access to services ends when
they are exhausted or the promotional window closes** — no invoice, no degraded
mode, the zone simply stops.

A default budget is the wrong instrument: it tracks net cost *after* credits,
which reads as roughly zero every month right up until access ends. Configure
it to exclude credits so it measures gross spend, then assert that it did:
if the credit setting reads true, the budget is measuring money owed and will
warn about nothing.

## 2. The image — no import required

Talos publishes AMIs per region, so there is no object-storage upload, no
import role, and no snapshot registration. Take the published image for the
architecture chosen in §0.

Published images carry **no system extensions**. The mesh client is added later
by upgrading to a factory installer built from a schematic (§8), not by
building a custom image.

## 3. A stable address

Allocate an Elastic IP **before generating the machine config**. The address is
baked into the cluster's certificates, and an ordinary public IP changes on
stop/start — which this instance will do when it is resized after the event.

## 4. Security group — inbound is temporary by design

Open the Talos API and Kubernetes API ports **from the workstation's address
only**, and treat those rules as scaffolding to be removed in §9.

**They must not stay.** The workstation's public address is the primary site's
static IP — the same address that changes when that site fails over to its
backup WAN. A rule pinned to it would deny access to the disaster-recovery
cluster during exactly the disaster it exists for.

## 5. Machine configuration

Generate with the Elastic IP as the endpoint, and list it in the additional
certificate SANs. Patch in: a schedulable control plane, no CNI (Cilium is
installed separately, as at the primary site), kube-proxy disabled, the install
disk, and the factory installer image carrying the mesh extension.

**Do not set a static hostname.** The platform supplies it and Talos rejects
the entire config — `static hostname is already set in v1alpha1 config` —
leaving the node in a config-acquire loop with its API never opening.

Validate before launching, because a rejected config is visible only on the
serial console:

    talosctl validate --config controlplane.yaml --mode cloud

## 6. Launch

Pass the machine config as user-data. Note that the launch call base64-encodes
a file for you while the *modify* call does not — it rejects a raw file and
needs the content already encoded.

Associate the Elastic IP once the instance is running; it is refused while the
instance is still pending.

Talos has no SSH, so the serial console is the only window into a boot that
goes wrong.

## 7. Bootstrap — endpoint public, node private

**This asymmetry is load-bearing.** `talosctl` reaches the *endpoint*, then asks
it to proxy to the *node* address. The cloud provider does not hairpin an
Elastic IP, so a node set to the public address makes the instance try to reach
itself and hang.

The symptom is thoroughly misleading: TCP connects, TLS completes, and the
client still reports `dial tcp <public-ip>:50000: i/o timeout` exactly as
though the port were filtered.

    talosctl config endpoint <elastic-ip>
    talosctl config node <private-ip>
    talosctl bootstrap
    talosctl kubeconfig ./kubeconfig --force

The node registers about 100 seconds later and reports `NotReady` until a CNI
exists, which is correct.

## 8. CNI, then the mesh

Install the Gateway API CRDs and Cilium using the same values as the primary
site, with one override: **the operator needs a single replica on a single
node**. The chart defaults to two with anti-affinity, so the second never
schedules and the install appears to fail on a timeout having actually
succeeded.

Then add the mesh client by upgrading to the factory installer image. This
reboots the node, and replaces the published image's own schematic — so any
extension it shipped with is gone, which is a removal rather than an addition.

**If that upgrade is interrupted, the node stays cordoned.** The tool cordons
before rebooting and uncordons afterwards; a killed client never reaches the
second half. It presents as pods stuck `Pending` with `node(s) were
unschedulable` on a cluster that otherwise reports healthy.

The mesh client takes its auth key through a machine config *document*, not a
v1alpha1 field. **The vendor's `curl | sh` installer cannot be used** — Talos
has no shell, no package manager and no `sudo`:

```yaml
apiVersion: v1alpha1
kind: ExtensionServiceConfig
name: tailscale
environment:
  - TS_AUTHKEY=<one-shot key>
  - TS_HOSTNAME=<node name>
  - TS_EXTRA_ARGS=--accept-routes
```

`--accept-routes` is what lets the node use the subnet routes the mesh
advertises. Without it the node joins the mesh and still cannot see the primary
site — which looks like success until something tries to use it.

Verify from a **pod**, not the node, since that is what the workloads do:

    kubectl run netcheck --image=<busybox> --restart=Never \
      --command -- sh -c "nc -z -w5 <primary-db> 5432 && echo REACHABLE"

## 9. Close the door

Add the mesh address to the certificate SANs first, or management over the mesh
fails certificate verification. Confirm both APIs answer over the mesh
**before** revoking anything, then remove every rule from §4.

The security group ends with **zero inbound rules**. Nothing needs to reach it:
the tunnel connector dials out, and so does the mesh client.

    nc -z -w6 <elastic-ip> 50000                       # must fail
    talosctl -e <tailnet-ip> -n <private-ip> version   # must still work

## 10. Cluster DNS must ask the primary site

The primary site's zone answers over **public DNS with proxy addresses, not
NXDOMAIN**. So a cluster using ordinary upstream DNS resolves the database name
to the CDN and connects to entirely the wrong thing — a failure that reads as a
database problem rather than a name-resolution one.

Point the zone at the primary site's own resolver, reachable over the mesh:

```
asn.casa:53 {
    errors
    cache 30
    forward . <internal-resolver> {
        max_concurrent 100
    }
}
```

Confirm from a pod that the data-tier names resolve to **private** addresses
rather than proxy ones. Cached wrong answers survive the reload, so re-check
after the cache TTL before concluding it failed.

This currently lives in the cluster's own CoreDNS ConfigMap and should move
into the GitOps tree with the rest of the zone's configuration.

## Living within the credits

Compute dominates and scales with instance size, so the instance type is the
lever: size for the event, shrink afterwards with a stop, a type change, and a
start, leaving the volume and its data intact.

The harder constraint is the end date. When the credits or the promotional
period run out, **access ends** — so this zone is a bridge, and its replacement
should be chosen well before that date rather than discovered on it.
