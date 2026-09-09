# `cluster/deimos/` — the second availability zone

A second Argo CD, in a different cluster, syncing **the same application
manifests as the primary** from the same paths in this repo. Only what genuinely
differs per zone lives here.

## Why there is no overlay tree

The obvious approach — Kustomize overlays patching copies of the app manifests —
was considered and rejected (`docs/design-multi-az-boreas.md` §4b). Kustomize is
used nowhere else in this repo, and the divergence turned out to be small enough
not to justify it: the applications differ by a handful of environment values,
not by structure.

So the Applications below point at `cluster/apps/spritz-production/...`, exactly
the paths the primary's own Applications use. Two Argo instances, two clusters,
one set of manifests. An image bump reaches both zones with no action, which is
the property that keeps them from drifting apart.

## How per-zone values work

Each app's containers carry an **optional** third `envFrom` entry:

```yaml
- configMapRef:
    name: cluster-overrides
    optional: true
```

On the primary that ConfigMap does not exist and `optional` makes the reference a
no-op. Here, `core/spritz-overrides.yaml` supplies it, and because it is last in
`envFrom` its keys win. Anything it does not mention falls through unchanged.

Verified behaviour, not assumed: with two ConfigMaps listed in that order the
later value wins and untouched keys pass through.

## What this zone deliberately does not run

**Production namespaces only, and initially only spritz.** Measured on the
primary: `spritz-production` uses about 1.8 GB and `asn-production` about 1.8 GB,
against 8 GB total here once a control plane (~2.5 GB) and the data tier are
accounted for. Both would fit but leave little headroom; the wedding site is the
thing with a fixed date, so it goes first.

Adding the other production namespace later is one Application file. Staging is
excluded permanently — it does not fit and is not in scope for disaster recovery.

**A consequence to expect:** during a failover this zone receives every hostname
the tunnel serves, including ones it has no workloads for. Those will 404. That is
correct rather than broken.

## Everything here is prefixed `deimos-`

That is a safety property, not a naming convention. Without it, this tree's
Applications and AppProjects share names with the primary's — and applying these
files to the wrong cluster would silently repoint the primary's own root at this
path, orphaning every application it manages.

A collision check is cheap and worth keeping in mind when adding files here: no
`Application` or `AppProject` name in this directory may match one elsewhere in
`cluster/`.

## Dependency worth knowing

The app cannot serve correctly against a read-only standby until the degraded-mode
work lands in the application repo — Rack::Attack writes to the database on nearly
every request via Solid Cache, so browsing itself fails first. See that repo's
`docs/design-degraded-read-only-mode.md`. The manifests here are still correct and
deployable in the meantime; the zone simply is not useful until both halves exist.
