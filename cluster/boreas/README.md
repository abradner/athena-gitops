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
| `boreas-*-shared` | the namespaces' image-pull secrets | |

Not here, on purpose:

- **Staging, everywhere.** It doesn't fit and isn't in disaster-recovery scope
  (§4d). `boreas-workloads` can write only to the two production namespaces, so
  that rule is enforced by the project rather than by convention. During a
  failover, staging hostnames reach this zone and get a 404. That's correct.
- **chuvar, counta and crm, for now.** Their images are published for arm64
  only, and this zone is amd64. Each comes back here (counta without
  `purge-cronjob.yaml`, crm without its Temporal worker and schedules hook)
  once its publish workflow adds amd64. Checked image by image on 2026-11-10:
  spritz, spritz-marketing and mcg carry both.
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

- Actions that start a Temporal workflow fail here.
- The web tiers run `db:prepare` on start, against the primary. Rails holds an
  advisory lock for migrations, so whichever zone starts first applies them
  and the other finds nothing pending.

## How per-zone values work

spritz's containers carry an **optional** last `envFrom` entry,
`cluster-overrides`. On the primary that ConfigMap doesn't exist, and
`optional` makes the reference a no-op. Here, `core/spritz-overrides.yaml`
supplies it. Because it comes last, its keys win.

**It holds one key today, `RAILS_MAX_THREADS`.** Both keys §4b expects wait on something
that doesn't exist yet: this zone's own object-storage node, and a Temporal
frontend reachable from outside the primary cluster. The file says which key
lands when. The asn-production apps carry no such reference yet. mcg needs
none.

## Other per-zone files

| File | Differs because |
|---|---|
| `cloudflared/deployments.yaml` | the connectors are held at **0** (§3). Derived from the primary's file, replicas excepted |
| `core/shared-from-primary.yaml` | tunnel config, credentials and the redirect route, read straight from the primary's paths |
| `core/gateway.yaml`, `core/certificates.yaml` | production listeners only. Derived copies |
| `core/observability.yaml` | `cluster: boreas` on every series, remote-written to the out-of-cluster tier |
| `core/external-secrets.yaml`, `secret-store/` | the same vault, this zone's own service-account token. The store has its own Application with retries, because on the first sync it was applied before ESO's webhook answered |
| `core/cloudflare-tokens.yaml` | this zone's own DNS-01 tokens, under the Secret names the shared issuers use. Cloudflare tokens carry an IP filter, and the primary's allow only the primary site |
| `core/spritz-overrides.yaml`, and `hpa.yaml` excluded | one spritz web pod with two threads: a warm standby with a small footprint on the shared database |

**One web pod each, small pools.** Each web pod here holds its own
connections to the primary's database (no pooler yet), so the standby is kept
small: spritz runs without its autoscaler and budget (`hpa.yaml` is
excluded), so one pod, and `RAILS_MAX_THREADS=2` in its override caps
threads and pool size. That caps it at two connections per database the pod
touches. mcg runs one pod with its default three threads. Scale up by hand
when this zone takes traffic (below).

**Derived copies name their source and move together.** The header of each
copy says which file it follows. An image bump to the connectors, a new
production listener for an app that also runs here, or a chart version bump
on the primary belongs in this tree in the same commit.

## Failing over to this zone

The watchdog that would do this (design Phase 5) doesn't exist yet. Until it
does, it's done by hand, from a host that can reach this cluster's API:

```bash
kubectl -n cloudflared scale deploy/cloudflared-personal deploy/cloudflared-simplytics --replicas=1
kubectl -n spritz-production scale deploy/spritz-web --replicas=2   # if it needs more than one pod
# back to the primary:
kubectl -n cloudflared scale deploy/cloudflared-personal deploy/cloudflared-simplytics --replicas=0
```

`boreas-cloudflared-connectors` ignores `spec.replicas`, so Argo doesn't undo
this. Promotion of the database standby is a separate, deliberate step, and so
is turning on the background side above.

## Bootstrap (once)

From a host with this cluster's kubeconfig:

Prerequisites, already true on the running cluster and part of the node's
build (private repo): Cilium with this repo's `bootstrap/kubernetes/cilium-values.yaml`
and the Gateway API CRDs. Nothing below works on a cluster without a CNI.

0. Apply the node's machine config with kubelet `serverTLSBootstrap: true`
   (private repo). Without it metrics-server can't verify the kubelet, and
   spritz's HPA, and with it the Application, never goes healthy.
1. `helm repo add argo https://argoproj.github.io/argo-helm --force-update`, then `helm install argocd argo/argo-cd -n argocd --create-namespace --version <the primary's pin> -f bootstrap/kubernetes/argocd-values-boreas.yaml`. Argo isn't self-managed here: upgrade it with the same command.
2. Load this zone's 1Password service-account token as `external-secrets/op-token` (key `token`) with `EXPECT_CONTEXT=boreas bootstrap/kubernetes/load1p-account-token.sh` (token from the environment; it creates the namespace first and refuses any other cluster). Never pass the token through argv.
3. Apply the zone's LB pool (private repo), then `kubectl apply -f cluster/boreas/root.yaml`.

## Everything here is prefixed `boreas-`

That's a safety property, not tidiness. No `Application` or `AppProject` name
in this directory may match one elsewhere in `cluster/`. Applying a
same-named root to the wrong cluster would repoint that cluster's own root at
this path.
