# `cluster/boreas/`: the secondary availability zone

A second Argo CD, in the boreas cluster, syncing **the same application
manifests as the primary** from the same paths in this repo. Only what
genuinely differs per zone lives here. Design: `docs/design-multi-az-boreas.md`
(§4b option 2 for per-zone values, §4d for scope). The site, host and network
specifics live in the private infrastructure repo.

## Why boreas runs its own Argo

The alternative was registering boreas as a remote cluster of the primary's
Argo CD. Rejected: a disaster-recovery zone whose reconciliation stops when the
primary stops fails exactly when it is needed. boreas pulls this repo from
GitHub itself and needs nothing from the primary to keep its workloads as
declared.

## What runs here

**Production web tiers only.**

| Application | Path | Left out |
|---|---|---|
| `boreas-spritz-production-spritz` | `cluster/apps/spritz-production/spritz` | `solid-queue.yaml`, `temporal-worker.yaml`, `transcoder.yaml` |
| `boreas-spritz-production-marketing` | `cluster/apps/spritz-production/marketing` | |
| `boreas-asn-production-mcg` | `cluster/apps/asn-production/mcg` | |
| `boreas-asn-production-chuvar` | `cluster/apps/asn-production/chuvar` | |
| `boreas-asn-production-counta` | `cluster/apps/asn-production/counta` | `purge-cronjob.yaml` |
| `boreas-asn-production-crm` | `cluster/apps/asn-production/crm` | `temporal.yaml` |
| `boreas-*-shared` | the namespaces' image-pull secrets | |

**Every image here must carry amd64.** This zone is Intel; the primary is
arm64. A single-arch image still publishes a manifest *list*, one entry long,
that looks healthy and fails only at pull time here. Before adding an app,
check: `docker manifest inspect <image> | jq -r '.manifests[].platform.architecture'`.
Checked 2026-11-10 for every image above: spritz, spritz-marketing and mcg
already carried both; chuvar, counta and crm gained amd64 that day.

Not here, on purpose:

- **Staging, everywhere.** It doesn't fit and isn't in disaster-recovery scope
  (§4d). `boreas-workloads` can write only to the two production namespaces, so
  that rule is enforced by the project rather than by convention. During a
  failover, staging hostnames reach this zone and get a 404. That's correct.
- **kaff.** Its hostname is under `asn.casa`, which the tunnel refuses by
  design, so a copy here would serve nothing. It also runs Solid Queue inside
  Puma, so a second copy would process jobs from this zone.
- **atmosphere and openwebui.** They're internal and LAN-only, and depend on
  hardware and services at the primary site.
- **The Temporal servers.** Two servers against one persistence store corrupt
  it, and their schema and namespace Jobs would run against the primary's
  database from here.

### Warm web tier only

Each excluded file holds the zone's background side: queue workers, Temporal
workers, cron. In normal running the primary does that work against the same
database. A second copy here would fire cron twice, run jobs across the WAN,
and CrashLoop the Temporal workers, which have no frontend to reach. They come
on at promotion, by deleting their names from the `exclude` in the
Application file. That's a reviewable one-line change, not a `kubectl scale`
that nobody records.

What that costs while this zone is serving and the primary isn't promoted:

- Actions that start a Temporal workflow fail here. For crm that means a
  contact-form notification is late, not lost: the submission is stored
  first, and the primary's sweeper delivers it.
- The web tiers run `db:prepare` on start, against the primary. Rails holds an
  advisory lock for migrations, so whichever zone starts first applies them
  and the other finds nothing pending.

## How per-zone values work

spritz's containers carry an **optional** last `envFrom` entry,
`cluster-overrides`. On the primary that ConfigMap doesn't exist, and
`optional` makes the reference a no-op. Here, `core/spritz-overrides.yaml`
supplies it. Because it comes last, its keys win.

**It is empty today, deliberately.** Both keys §4b expects wait on something
that doesn't exist yet: this zone's own object-storage node, and a Temporal
frontend reachable from outside the primary cluster. The file says which key
lands when. The asn-production apps carry no such reference yet. mcg,
chuvar and counta need none. crm's would be its Temporal address, which
waits on the same reachable frontend as spritz's.

## Other per-zone files

| File | Differs because |
|---|---|
| `cloudflared/deployments.yaml` | the connectors are held at **0** (§3). Derived from the primary's file, replicas excepted |
| `core/shared-from-primary.yaml` | tunnel config, credentials and the redirect route, read straight from the primary's paths |
| `core/gateway.yaml`, `core/certificates.yaml` | production listeners only. Derived copies |
| `core/observability.yaml` | `cluster: boreas` on every series, remote-written to the out-of-cluster tier |
| `core/external-secrets.yaml` | the same vault, this zone's own service-account token |

**Replica counts are the primary's.** §4d expected autoscaler minimums of one
here. Nothing patches them, since the node has the room. One cost to know:
on a single node, a drain can't satisfy a `minAvailable: 1` budget (the
replacement pod has nowhere to go), so anything that drains this node waits
on those budgets.

**Derived copies name their source and move together.** The header of each
copy says which file it follows. An image bump to the connectors, a new
production listener for an app that also runs here, or a chart version bump
on the primary belongs in this tree in the same commit.

## Failing over to this zone

The watchdog that would do this (design Phase 5) doesn't exist yet. Until it
does, it's done by hand, from a host that can reach this cluster's API:

```bash
kubectl -n cloudflared scale deploy/cloudflared-personal deploy/cloudflared-simplytics --replicas=1
# back to the primary:
kubectl -n cloudflared scale deploy/cloudflared-personal deploy/cloudflared-simplytics --replicas=0
```

`boreas-cloudflared-connectors` ignores `spec.replicas`, so Argo doesn't undo
this. Promotion of the database standby is a separate, deliberate step, and so
is turning on the background side above.

## Bootstrap (once)

From a host with this cluster's kubeconfig:

0. Apply the node's machine config with kubelet `serverTLSBootstrap: true`
   (private repo). Without it metrics-server can't verify the kubelet, and
   spritz's HPA, and with it the Application, never goes healthy.
1. `helm install argocd argo/argo-cd -n argocd --create-namespace --version <the primary's pin> -f bootstrap/kubernetes/argocd-values-boreas.yaml`. Argo isn't self-managed here: upgrade it with the same command.
2. Load this zone's 1Password service-account token as `external-secrets/op-token` (key `token`) with `EXPECT_CONTEXT=boreas bootstrap/kubernetes/load1p-account-token.sh` (token from the environment; it creates the namespace first and refuses any other cluster). Never pass the token through argv.
3. Apply the zone's LB pool (private repo), then `kubectl apply -f cluster/boreas/root.yaml`.

## Everything here is prefixed `boreas-`

That's a safety property, not tidiness. No `Application` or `AppProject` name
in this directory may match one elsewhere in `cluster/`. Applying a
same-named root to the wrong cluster would repoint that cluster's own root at
this path.
