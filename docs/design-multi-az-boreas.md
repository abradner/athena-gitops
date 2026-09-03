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
cache so the VMs fit, reserve addresses, publish service names under
`<dr-zone-domain>`, and join the mesh VPN with a route advertisement and an ACL
narrow enough to permit only the replication ports. Documented in the private
infrastructure repo.

**Phase 2 — State replication.** A PostgreSQL hot standby and a second Garage
node at the DR site, both on the hypervisor tier, both reached over the mesh
VPN. The standby is fronted by a local proxy so that promotion is a
configuration flip rather than an application change. Replication lag is a
metric with an alert.

**Phase 3 — The boreas Talos cluster.** Two virtual machines at the DR site: a
schedulable control plane and one worker. This requires parameterising the
Talos machine-config templates, which currently hardcode the cluster name,
endpoint, install disk and network interface. Athena's rendered output must be
byte-identical after the change — verify with a diff before merging.

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
