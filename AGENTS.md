# Agent Onboarding: Athena GitOps

## Repository Overview
`athena-gitops` is the foundational cluster management repository for the Athena homelab. It maintains raw Kubernetes bootstrap assets, ArgoCD Application manifests, and core cluster infrastructure configuration. A related, separate repository — `tools-workflow` — provides the automation pipeline that drives several operational workflows against this repo.

## Domain Nomenclature
- **Application**: An individual microservice (e.g., `pmn-ext-gw`), mapping directly to the ArgoCD Application schema.
- **Project**: The overall deployment suite scope (e.g., `pmn`). `Config#project_name` is the canonical identifier, sourced from the `PROJECT_NAME` env var and used for namespace interpolation (`#{project_name}-#{env}`).

## Placement Doctrine — what runs where, and why

The single most load-bearing set of decisions in this estate. Stated as principles; the
concrete inventory (which guest runs on which host, addresses, ids) lives in the **private**
`asn-infra` repo, deliberately not here — see "The public/private split" below.

**1. Prod-facing applications run in-cluster; their state does not.** Every app here is
stateless against an external PostgreSQL and an external S3-compatible object store. This is
not incidental: during a month-long cluster outage in 2026, data loss was zero *because* of
this split, and the refresh/restore tooling stays simple because the database is a plain host
rather than a cluster-managed service.

**2. State lives on the hypervisor tier, never on the storage box, never on the Pi
hypervisors.** The hypervisor cluster has the RAM, NVMe mirrors and its own backup server, and
is a separate failure domain from the cluster. The NAS is the *backup destination* — putting a
source there defeats the purpose. The Pi hypervisors that host the control-plane VMs would
contend for memory with those VMs and couple two failure domains.

**3. Nothing that watches the cluster may depend on the cluster.** Monitoring, alerting and
backups run outside it. A detector living inside its subject fails by going silent, which is
indistinguishable from "everything is fine" — this is exactly how the month-long outage stayed
invisible. Only stateless collectors (metric scraper, log shipper, exporters) remain in-cluster;
they buffer to disk and forward outward.

**4. A cache is a third case, not "state".** Durability, survivability and latency are separate
axes and a cache scores differently on each: rebuildable (no durability need), useless while the
cluster is down (no survivability need), hit on every request (latency matters). All three point
**in-cluster**. The exception that flips it: a Redis used as a *job queue* is not
reconstructible and is a database in disguise.

### Rejected alternatives, with the reasons

Recorded so they are not re-proposed as if new.

- **NFS-backed PersistentVolumes from the NAS.** Requested with the condition "as long as it
  won't take down the cluster if the NAS is away for a few hours". That condition is not
  satisfiable: hard mounts wedge kubelet's volume manager, and soft mounts corrupt a
  time-series database mid-write. Inverted to node-local volumes plus scheduled snapshots
  outward, keeping the NAS out of the runtime path entirely.
- **Longhorn and Ceph.** Declined — both add a distributed-storage control plane (and its
  failure modes, and its own backup obligation) to solve a problem the stateless-app split
  already removes.
- **A tainted in-cluster worker for observability.** Solves capacity but not survivability:
  control-plane death still blinds you. Survivability was the whole point.
- **Moving metrics/logs into PostgreSQL** to reduce the number of things to back up. Argues the
  other way: observability data is derived and deliberately disposable, and putting it in the
  database makes it look like something that must be protected.

## File Layout & Separation of Concerns

### `bootstrap/`
Segmented into `talos/` and `kubernetes/`:

- **`talos/`**: Contains `.template.yaml` files with `{{ dotted.key }}` placeholders (e.g., `{{ certs.os.crt }}`). These are rendered into hydrated YAML configs via the `render-talos` command in `tools-workflow`, which reads a single 1Password Secure Note (identified by `OP_TALOS_ITEM_ID` in `tools-workflow/.env`), flattens its YAML contents into dot-separated keys, and substitutes all placeholders. Generated files are gitignored — they contain full cluster PKI and must never be committed.
- **`kubernetes/`**: Provisioning scripts for core networking, Cilium, ArgoCD, and the Gateway.

See [`bootstrap/README.md`](bootstrap/README.md) for the full order of operations.

### `cluster/` (top level)
Synced non-recursively by the self-managing root app (`cluster/root.yaml`, prune off): the `core`/`pmn` AppProjects (`projects.yaml`) and the two child Applications `cluster-core.yaml` / `cluster-apps.yaml`, which recursively own `cluster/core` and `cluster/apps` with automated prune + self-heal. New Applications must set `project: core` (this repo / pinned chart repos) or `project: pmn` (`pmn-workloads` → `pmn-*` namespaces) — not `default`.

### `cluster/apps/`
Overarching ArgoCD sync targets linking ArgoCD to upstream workload repositories (e.g., `pmn-workloads`). The `pmn` suite is defined by a single `ApplicationSet` (`pmn-appset.yaml`) whose matrix generator expands service × environment lists into per-app `Application` resources — add an environment or service by appending to the relevant list, not by adding per-env manifest files.

### `cluster/core/`
Foundational cluster integrations:
- `onepassword-backend` `ClusterSecretStore` for ExternalSecrets retrieval via 1Password.
- ArgoCD GitHub repository authentication via an `ExternalSecret` (`argocd-github-repo-pmn.yaml`) that maps a GitHub PAT from 1Password into the ArgoCD control plane.

## Agent Guidelines & Operation
- **Argo App Instantiation**: The `pmn` ApplicationSet in `cluster/apps/pmn-appset.yaml` generates sync targets per application and environment. (Historically the `GenerateArgocd` orchestrator in `tools-workflow` emitted per-environment manifest files; if that tool still regenerates `pmn-dev*.yaml` files it must be updated to edit the ApplicationSet lists instead, or its output will conflict with the ApplicationSet-owned Applications.)
- **Syntactic Validation Only**: This repository uses syntactical YAML verification. Do not attempt behavioural spec logic. A `.git/hooks/pre-commit` hook uses `yq` (via `mise.toml`) to run `./scripts/validate_yaml.sh`, preventing structurally invalid commits.
- **Do Not Hardcode Secrets**: All sensitive values must be templated and sourced from 1Password. If you encounter plaintext certs or tokens in committed files, flag them immediately.

## Recent Snapshot Learnings & Insights
1. **1Password API Rate Limiting**: High workload counts can cause ExternalSecret refresh cycles to exceed 1Password's rate limits. `refreshInterval` has been scaled to `24h` / `6h` cycles to reduce authentication pressure.
2. **Application-Scoped Resources**: Centralising shared resources (e.g., `registry-pull-secret`) into a singular infra app disrupts ArgoCD sync dependencies. Resources are scoped per-application (e.g., `pmn-ext-gw-registry`) to maintain clean ownership topologies.
3. **Cluster-Level Networking**: External hostnames for cluster-level services (such as ArgoCD and Hubble UI) use the `*.athena.asn.casa` domain. These hostnames are typically secured via local HAProxy configurations to remain visible only on the local LAN.

## Argo Self-Management Traps (hard-won, 2026-08)

Six distinct failure modes hit while operating this cluster. Each looks like a
different bug; all are Argo behaving as designed in ways that mislead.

1. **A pinned sync operation survives hard refresh.** Merges appear to do
   nothing for 20+ minutes while Argo retries an old revision. Fix:
   `kubectl patch application <app> -n argocd --type json -p '[{"op":"remove","path":"/operation"}]'`

2. **The Application CRD schema silently prunes unknown fields.** Asserting a
   field this Argo version's schema lacks (e.g. `spec.source.helm.kubeVersion`)
   creates a permanent sync loop: git asserts it, the live object can never
   hold it. Fix by removing the field from git, never by re-syncing harder.

3. **Go zero-values are dropped on serialization.** Asserting
   `directory.recurse: false` explicitly loops forever for the same reason —
   `false` is Go's zero-value for bool and is never serialized back, so live
   never matches git. `true` persists fine; the asymmetry is the tell. Omit
   zero-value fields instead of asserting them.

4. **A completed hook Job never re-runs, even when its manifest changes.**
   `Synced` at the current revision does not mean a sync *operation* ran, and
   hooks fire only on operations. A PostSync Job (e.g. Temporal namespace
   registration) that Completed before a new consumer appeared will look green
   and will not have run. Fix: delete the Job, then force a real operation —
   `kubectl patch application <app> -n argocd --type merge -p '{"operation":{"sync":{"revision":"<rev>"},"initiatedBy":{"username":"manual"}}}'`
   (a hard-refresh annotation alone does not trigger hooks). Expect this for
   every new app added to the shared Temporal cluster.

5. **"one or more synchronization tasks are not valid" ≠ "completed
   unsuccessfully".** "Not valid" means the AppProject denied it (check the
   project's permitted destination namespaces); "unsuccessfully" means the
   apply itself failed. They need opposite responses.

6. **A `Synced` CR can mean nothing was deployed.** Argo only knows the CR
   applied cleanly, not that its operator could act on it. Read the CR's
   `.status.reason` (e.g. a generated Service name over 63 characters kept a
   `VMAlertmanager` "Synced" and dead for hours).

## Gotchas & Lessons Learned

Numbered and accreting — add entries rather than rewriting, so they can be cited from code
comments and PR descriptions. Entry shape: **what happened → the actual root cause → the
general rule.** Argo-specific traps have their own section above.

1. **A Helm `null` override did not delete an inherited key; it shipped the word `null`.**
   Pruning the in-cluster observability stack left the log shipper's chart-default sink behind
   with an empty endpoint, which is fatal at boot — log collection was down cluster-wide. The
   fix attempt asserted `<sink>: null` to remove it. Root cause: **Helm's deep merge can add
   keys but never remove them**, and this chart passes its `customConfig` through to the
   rendered output, so the null serialized literally and stayed a sink. General rule: to
   neutralize an inherited chart value, *complete* it into something valid rather than trying
   to delete it — and never assume a value's absence from your own values file means absence
   from the rendered output.

2. **`render before merge`, not `merge and watch`.** The above cost two merges, two Argo syncs,
   a DaemonSet roll and roughly ten extra minutes of outage to discover something a local
   `helm template` would have shown in one minute. A later commit in the same incident also
   silently deleted a control-plane toleration and a resources block, because the edit replaced
   a text span that reached past its intended end. General rule: for any chart-value change,
   render the chart locally and inspect the *rendered* object — and diff the whole change, not
   just the part you were thinking about. Verifying the previous failure mode is not the same
   as reviewing the diff.

3. **A DHCP-supplied domain became a pod DNS search domain and silently blackholed traffic.**
   Control-plane pods could not reach an out-of-cluster service; the client reported request
   timeouts, no packets ever reached the destination host, and the CNI logged no drops. Root
   cause: the control-plane VMs receive an FQDN by DHCP, from which Talos derives a node search
   domain that kubelet passes to every pod on that node. With the default `ndots:5`, a service
   hostname was tried **with the search suffix first**, that suffixed name matched a public
   wildcard DNS record, and the connection went to a CDN edge that accepts and drops the port.
   Worker nodes had no such domain, so only control-plane pods were affected. General rule: two
   defences, both applied — set `machine.network.disableSearchDomain: true` on nodes whose DHCP
   supplies a domain (see `bootstrap/talos/controlplane.template.yaml`), and write in-cluster
   references to names under a wildcarded zone as absolute FQDNs with a trailing dot. Corollary
   for diagnosis: connectivity probes by IP, and name lookups issued as absolute queries, both
   pass while the real path fails — reproduce the resolver's actual search behaviour from inside
   an affected pod.

## The public/private split

**This repository is public.** Internal addresses, guest inventories, host-to-service mappings
and anything else that describes the shape of the private network belong in the private
`asn-infra` repository, not here. When documenting a lesson whose specifics are topological,
write the transferable rule here and keep the inventory there.
