# Design — boreas, a second availability zone

Status: **approved, in progress.** Started 2026-09-03. Target: serving from two
sites before 2026-10-08.

Placeholders throughout — `<dr-site>`, `<dr-zone-domain>`, `<dr-host>`. Concrete
addresses, hardware and host state live in the private infrastructure repo, not
here. See the public/private split in `AGENTS.md`.

---

## 1. Why

The home site's upstream cable link faults intermittently. Outages last around
five minutes and recur. The router fails over to a 4G WAN, but every public
hostname is a **proxied Cloudflare record pointing at the static ISP address**,
so a WAN failover changes the origin address and the site stays dark until DNS
is edited by hand. Nothing about the cluster is broken during these events —
only the path to it.

A wedding site (`rabalex.wedding`, plus its marketing front on `spritz.events`)
is served from this cluster and has a fixed, unmovable date. Every other public
property has the same exposure; the wedding just makes the deadline real.

So: a second availability zone at a different physical site on a different
ISP, running the **whole cluster**, with athena preferred whenever it is
healthy. Named `boreas` (the north wind) to continue the Greek theme.

Failure modes, in the order they actually happen:

| # | Failure | Frequency | Response |
|---|---|---|---|
| 1 | Home WAN flaps, cluster fine | Weekly, ~5 min | Automatic, seconds |
| 2 | Home site hard-down (power, hardware) | Rare | One manual command |
| 3 | DR site down | Rare | Nothing — athena serves alone |

The design optimises for #1, survives #2 with a single documented command, and
costs nothing during #3.

## 2. What the topology already gives us

Three existing decisions make a second AZ far cheaper than it would otherwise
be, and they constrain the design:

- **All state lives outside Kubernetes.** One PostgreSQL instance and one
  Garage S3 instance run on the hypervisor tier, and every app is stateless
  against them (`AGENTS.md`, placement doctrine #1). Replicating the AZ is
  therefore a hypervisor-tier problem, not a Kubernetes one — no distributed
  storage layer, no CSI, no operator failover.
- **One PostgreSQL instance serves every app.** Physical streaming replication
  mirrors the entire instance, so every application's database comes along for
  free. There is no per-app replication to configure.
- **Observability is already out-of-cluster** (placement doctrine #3). The
  stateful half runs on the hypervisor tier at the home site, so boreas ships
  metrics and logs to the existing collectors rather than standing up a second
  stack.

## 3. Target architecture

```
        Cloudflare edge (both accounts, proxied DNS)
       tunnel "athena-personal"     tunnel "athena-simplytics"
         /            \                /            \
   cloudflared     cloudflared    cloudflared    cloudflared
   athena x2       boreas 0/1     athena x2      boreas 0/1
   (always on)     (watchdog)     (always on)    (watchdog)
         |               |              |              |
     Cilium Gateway  Cilium Gateway   (the same two gateways)
     all HTTPRoutes  all HTTPRoutes
         |               |
         |               `--- DB, Temporal via mesh VPN ---> athena
         |
   postgres (primary)  <--- streaming replication --->  postgres (hot standby)
   garage zone=athena  <--- replication factor 2  --->  garage zone=boreas
```

**Cloudflare Tunnel replaces the static-IP dependency.** Connectors dial *out*
to Cloudflare, so the origin address stops mattering: on a WAN failover the
athena connectors simply reconnect over 4G and the public hostname never
changes. This alone fixes failure mode #1 without boreas existing at all, which
is why it ships first.

Two tunnels are needed, not one: a tunnel belongs to a Cloudflare account, and
the served zones are split across two accounts (see `AGENTS.md` and the
cert-manager issuer config, which already carries two DNS-01 solvers for the
same reason).

### Serving policy

Athena serves while it is green. A watchdog scales the boreas connectors from
zero to one when athena's origin fails a direct probe for 30 s, or immediately
when the router reports the primary WAN down. It scales them back to zero after
athena has been green for 15 s, with a cap of one flip per minute. Cloudflare
drops a dead connector from the tunnel within seconds, so no DNS record is ever
touched.

### Database policy

Athena's PostgreSQL is **always** the primary. Boreas pods write to it across
the mesh VPN, which re-homes over 4G during a flap. Promotion of the standby is
**manual and deliberate**, reserved for failure mode #2.

This is the single most important constraint in the design. A five-minute WAN
flap must never trigger a promotion: automatic promotion over a flapping link
produces split-brain and a `pg_rewind` ping-pong, and the applications cannot
run against a read-only database for long enough to make a middle path safe.
The mitigation for the wedding day is that promotion is one command over the
mesh VPN, runnable by someone other than the operator.

### Object storage policy

One Garage cluster, two zones, `replication_factor = 2`, write quorum 1. A
write succeeds at whichever zone is reachable and syncs to the other when the
link returns. Stored objects are immutable and uniquely keyed, so relaxed write
consistency introduces no conflict semantics. Reads are served locally in each
zone.

### Deliberately not doing (revisit after 2026-10-08)

Recorded so they are not re-proposed as if new, per the doctrine convention:

- **A third zone in a free cloud tier**, acting as tie-breaker and as a third
  Garage replica. It would let Garage keep read+write quorum through any single
  site loss and give the watchdog a vote from outside both houses. Rejected for
  now purely on time: it adds a third thing to maintain before the deadline.
- **Automatic database promotion.** See the database policy above — this is
  rejected on correctness for flapping links, not only on effort. It becomes
  reasonable only with an out-of-band third opinion, i.e. after the third zone
  exists.
- **Moving the DR data tier to system containers.** The platform's container
  support would give the standby the same packaging, the same paths and the
  same promotion commands as the primary, which is worth real money at 2am. It
  is deferred purely on sequencing: it is an unexercised subsystem on that host
  and this is the site that has to work when everything else does not. Revisit
  once the deadline is behind us.
- **Per-hostname or weighted traffic steering.** Cloudflare load balancing is a
  paid feature, and the free path (connector present or absent) is all-or-
  nothing per tunnel. Acceptable: the zones are equivalent.

## 4. Build phases

Each phase is independently useful and independently revertible.

**Phase 0 — Cloudflare Tunnel on athena.** Resolves failure mode #1 on its own,
before any second site exists. Two connector Deployments in `cluster/core/`,
tokens from 1Password via External Secrets, pointed at the existing Gateway.
Cut staging hostnames first, force a WAN flap, then cut the rest. Internal-only
hostnames stay on the edge proxy and never enter a tunnel. Rollback is a DNS
record.

### Pre-flight findings (2026-09-03)

Measured before committing to the phase order; both change the effort estimate.

**PostgreSQL is already configured for replication.** `wal_level` is `replica`
and `max_wal_senders` is 10, so standing up a standby needs no restart of the
primary and no downtime — only a replication slot, a role, and a host-based
auth line. There are no replication slots yet. The whole instance, every
application's databases together, is about 300 MB, so a base backup runs in
seconds and can be retaken freely rather than treated as a careful operation.

**Garage needs two changes that both require a restart.** It runs a single node
with `replication_factor = 1`, and its RPC public address is loopback — as it
stands no second node could connect at all. About 134 MB across two buckets, so
the rebalance after the layout change is trivial. Only one application uses
object storage today, which narrows what Phase 2 has to prove.

Neither restart is disruptive on its own, but they land on the data tier, so
they belong in a quiet window and not on the same day as anything else.

**Phase 1 — DR site preparation.** Host-level work at `<dr-site>`: resolve a
degraded storage pool before anything is placed on it, bound the filesystem
cache so the guests fit, reserve addresses, publish service names under
`<dr-zone-domain>`, and join the mesh VPN with a route advertisement and an ACL
narrow enough to permit only the replication ports. Documented in the private
infrastructure repo.

The one genuinely risky step in the whole build lives here. The existing guest
on that host is attached by **macvtap, which blocks guest-to-host traffic** —
and the cluster's pods must reach the data tier on the host. So the host needs
a real **bridge** on its single network interface. That change is applied
remotely, to the only path in, at a site with no console access. The platform
offers test-and-revert on network changes; use it, and have someone at the site
before starting.

**Phase 2 — State replication.** A PostgreSQL hot standby and a second Garage
node at the DR site, both on the host tier rather than inside Kubernetes, both
reached over the mesh VPN. The standby is fronted by a local proxy so that
promotion is a configuration flip rather than an application change.
Replication lag is a metric with an alert.

Both run as **containerised services on the host**, pinned to the exact
upstream versions the primaries run. The platform now also offers system
containers, which would match the primary site's packaging and give identical
promotion commands — the better long-term answer, and deliberately not taken
yet (see the deferred list). The DR site is the thing whose entire purpose is
to work when the primary is broken; it should not be the first user of the
newest subsystem on a pre-release build, five weeks from a fixed date. Exact
version pinning is also strongest with images, and the standby must match the
primary's minor version.

**Phase 3 — The boreas Talos cluster.** **One** virtual machine at the DR site,
a schedulable control plane. A second node on the same physical host would buy
no availability — only memory and another thing to patch — so the cluster is
deliberately single-node. In-place draining is given up at this site, which the
primary covers.

This requires parameterising the Talos machine-config templates, which
currently hardcode the cluster name, endpoint, install disk and network
interface. Athena's rendered output must be byte-identical after the change —
verify with a diff before merging.

**Phase 4 — GitOps for boreas.** A `cluster/boreas/` tree of Kustomize overlays
that reference athena's existing manifests as bases and patch only what differs
per zone: data-tier hostnames, autoscaler minimums, the observability cluster
label, and the connector replica count. Athena's own tree is untouched, and its
root Application excludes the overlay directory so the two clusters never fight
over the same resources. Boreas runs its own Argo CD.

One asymmetry cannot be patched away: two Temporal servers must never share one
persistence store. Boreas' Temporal servers stay at zero replicas until
promotion, and its workers connect to athena's Temporal frontend over the mesh
VPN.

**Phase 5 — The watchdog.** One script on the hypervisor tier at the home site,
outside the cluster it watches (placement doctrine #3), extending the existing
watchdog host. It probes athena's origin directly, reads the router's WAN
state, and scales the boreas connectors. It is the only thing with authority to
change which zone serves.

## 4b. Open decision — how boreas gets its manifests

Phase 4 originally said "Kustomize overlays". That was written before checking
two things, and both undercut it.

**Kustomize is used nowhere in this repo.** Every manifest is plain YAML
recursed by Argo, with Helm only for upstream charts and one ApplicationSet for
a suite that is currently on ice. Introducing an overlay tree would add a tool,
and a second way of reading any given manifest, to a repo whose whole
navigability rests on there being one.

**Far less differs per cluster than the phase assumed.** Measured against the
app config as it stands:

| Value | Differs on boreas? |
|---|---|
| Database host | **No.** Boreas writes to the primary over the mesh, so the same name is correct. Only promotion changes it, and that is a proxy flip, not a manifest edit. |
| Object storage endpoint | **Yes.** Each zone should read and write its local node. |
| Workflow-engine host | **Yes.** Boreas' own server stays at zero replicas, so its workers point at the primary site's frontend. |
| Canonical host, mailer host, bucket names | No. |
| Autoscaler minimums, connector replicas, the observability cluster label | Yes, but these live in one file each, not per app. |

So the per-application divergence is **two environment variables**, not a tree.

### The options

1. **Kustomize overlays.** Sound, and the obvious answer in the abstract. Costs
   a new tool, and the bases were never written as bases — each overlay would
   list files individually and patch by selector.
2. **Per-cluster override ConfigMap.** Apps already load config via `envFrom`,
   so appending a second `configMapRef` lets one small per-cluster ConfigMap
   override individual keys. Verified on the live cluster rather than assumed:
   with two ConfigMaps listed in that order, the later one's value won and keys
   it did not mention passed through untouched. Costs one line per container,
   once, and every value stays greppable in plain YAML.
3. **Per-cluster DNS.** Let each cluster resolve the same names to different
   places, leaving manifests byte-identical. Elegant, and the most invisible
   when wrong — this repo already carries scars from a DNS resolution surprise.
4. **A duplicated tree.** Honest and dumb. Drifts by the second week.

### Recommendation

**Option 2.** It reuses a mechanism the apps already have, keeps every value
visible in plain YAML, adds no tool, and confines cluster divergence to one
small file per cluster that can be read in full in ten seconds. Option 1
becomes the better answer only if per-cluster divergence grows well beyond the
two variables measured above, and that is worth revisiting after the wedding
rather than assuming now.

The remaining per-cluster differences that are not app config — connector
replicas, the observability cluster label, workflow-engine replicas — are one
file each and can simply be separate files under a boreas directory.
## 4c. Revision, 2026-09-05 — Deimos becomes the second zone

The DR host lost a second drive from an already-degraded array, its pool
suspended, and the box is unreachable until someone can be physically present
about a week from now. Phase 1 said "resolve the degraded pool before anything
is placed on it"; nothing had been placed on it, which is the only good news
here. Treat the offsite copies that lived on that pool as unverified until
someone has looked.

**A cloud zone becomes the second AZ, and the house at the other site becomes
the third, after the wedding.** This reverses the earlier ordering. The
reasoning is not that cloud is better in the abstract — it is that the second
zone is available now, has independent power, network and hardware, and the
other site has just demonstrated it is the least reliable component in the
design. Deferring it also means object storage can go to three replicas later
without a redesign.

### Zone names

Lettered in the order they were conceived, which is not the order they will be
built:

| | Zone | Where | State |
|---|---|---|---|
| A | `athena` | primary site | serving |
| B | `boreas` | second house | blocked on hardware; becomes the third zone |
| C | `carcinus` | first cloud provider | **reserved** — never built, onboarding stalled |
| D | `deimos` | second cloud provider | the second AZ, being built now |

`carcinus` stays reserved rather than recycled. The provider it was named for
may still complete its onboarding, and two zones with a claim to one name is a
problem best avoided while both exist only on paper.

Deimos: dread, and a moon of Mars. Appropriate for the thing you only look at
when something has gone wrong.

### What made this cheap

**Every node in the primary cluster is arm64.** Control plane and workers
alike, so every application image already runs on Arm and the free Arm compute
that makes this possible needs no rebuilds. This was checked, not assumed.

Talos also ships first-class platform support for every cloud considered here,
and its image factory builds a matching Arm image with the mesh-VPN extension
included — which is what keeps the choice of provider a commercial decision
rather than an engineering one.

### The constraint that shaped it, and the provider that lost

The first choice of provider offered the only genuinely large free allowance —
12 GB of Arm — and then blocked on a sales-led onboarding queue with five weeks
to the date. Waiting on someone else's queue for a deadline you cannot move is
not a plan, so the zone moved to a provider whose signup completes immediately.

Every other free tier bottoms out at 1 GB instances, which would have reduced
this zone to replication with nowhere to serve from. What makes the current
provider workable is **promotional credits**, which buy a properly sized
instance rather than a token one. The catch is in the wording on the console:
access to services ends when the credits are exhausted **or** when the
promotional period ends. This zone is therefore a bridge with a known expiry,
not a permanent home, and its replacement should be chosen before that date.

So deimos is **one instance, 2 vCPU and 8 GB**, single-node Talos, with the
PostgreSQL standby and the object-storage node running as workloads inside the
cluster on node-local volumes.

8 GB is the floor, not a preference. The primary cluster's `kube-apiserver`
settles around 1.4 GB — the measurement that forced its control-plane nodes to
4 GB — so a node carrying a control plane, the workloads, a database standby
and object storage has about 1.5 GB left over at 4 GB total. That is not a
margin.

**This bends the placement doctrine, deliberately and narrowly.** The doctrine
keeps state out of Kubernetes so that rebuilding a cluster cannot lose data.
At this site both are *replicas* — re-seedable from the primary in minutes,
since the whole database is about 300 MB and the object data about 134 MB.
Nothing here is a source of truth, so the property the doctrine protects does
not apply. It still applies unchanged at the primary site, and at the third
zone when it returns.

### Architecture: resolved, and not a constraint

Every node in the primary cluster is arm64, so nothing there has ever forced an
amd64 build. That raised a worry about the third zone, which runs on Intel.

**Answered: the images are multi-arch.** The publish workflow assembles
per-architecture digests into a manifest list and explicitly guards against
publishing a single-arch manifest under the full tag set. So the third zone can
be Intel, and a cloud zone can be whichever architecture is cheaper or
available — Arm, for now, on price.

### The expiry, which is the real risk

Nothing here gets reclaimed for being idle. The exposure is simpler and
harder: the credits run down at a rate set by the instance size, and when they
are gone — or when the promotional window closes — **access to services ends**.
There is no invoice to ignore and no degraded mode; the zone stops.

Two consequences worth building around. A cost budget with alerting goes in
before the first instance, not after. And the instance is sized for the event
and shrunk afterwards, which is a stop, a type change, and a start, with the
volume and its data intact.

The date this zone expires should be in the calendar the day it is created.

## 4d. What a secondary zone actually runs

**Production namespaces only.** Staging and, later, development stay switched
off in every zone but the primary.

This is not tidiness, it is what makes the zone fit. Measured on the primary
cluster:

| | Pods | Memory in use |
|---|---|---|
| Production namespaces | 24 | 3.7 GB |
| Staging namespaces | 20 | 1.9 GB |

Against a secondary zone's 8 GB, with roughly 2.5 GB for a control plane whose
apiserver alone settles near 1.4 GB, and roughly 0.8 GB for the database
standby and object-storage node, production-only lands around 7 GB. Adding
staging needs about 9 GB and does not fit at all. Reduced replica counts in the
secondary claw back a little more, since the autoscaler minimums drop to one.

So the ceiling is not a preference to revisit if things get tight — it is the
reason the zone is possible on this footprint.

### Two consequences worth expecting

**The overlay gets simpler, not more complex.** A secondary zone's tree omits
the staging applications entirely rather than carrying them patched to zero
replicas. Nothing to keep in step, and no risk of a patch silently failing open
and scheduling a staging workload into a zone with no room for it.

**Staging breaks during a failover, by design.** Tunnel hostnames are served by
whichever connectors are registered, so when a secondary zone takes over it
receives staging hostnames too — and has nothing to answer them with. Expect a
404 for staging during any failover. That is correct behaviour rather than a
fault to debug: staging is not in the disaster-recovery scope, and a site
outage taking staging with it costs nothing.

The alternative — separate tunnels so staging never fails over — buys a tidier
error for a hostname nobody is looking at mid-incident, at the price of more
tunnels to hold in your head. Not worth it.

## 5. Verification

| Phase | Test | Pass |
|---|---|---|
| 0 | Disconnect the primary WAN for two minutes | Every public hostname answers 200 within 30 s over the backup WAN, no DNS edit |
| 2 | `pg_stat_replication` on the primary | Standby streaming, lag under a second |
| 2 | Upload via the DR Garage endpoint with the home Garage node stopped | Write succeeds; object readable from the home zone after it restarts |
| 3 | `talosctl`/`kubectl` against boreas | Both nodes Ready, Argo CD apps Synced and Healthy |
| 4 | Submit a real form through the boreas Gateway | Row lands in the home site's primary database |
| 5 | Disconnect the primary WAN | Boreas connectors scale to 1 within 30 s, site stays 200 throughout, connectors return to 0 after recovery, no promotion occurred |
| 5 | Power down the home hypervisors | Documented promotion command succeeds; writes served from boreas |

Before every push: review the file list. No site addresses, no guest
inventories, no hardware state in this repo.
