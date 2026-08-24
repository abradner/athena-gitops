# Antigravity & Gemini Agent Onboarding: Athena GitOps

## Repository Overview
`athena-gitops` is the foundational cluster management repository for the Athena homelab. It maintains raw Kubernetes bootstrap assets, ArgoCD Application manifests, and core cluster infrastructure configuration. A related, separate repository — `tools-workflow` — provides the automation pipeline that drives several operational workflows against this repo.

## Domain Nomenclature
- **Application**: An individual microservice (e.g., `pmn-ext-gw`), mapping directly to the ArgoCD Application schema.
- **Project**: The overall deployment suite scope (e.g., `pmn`). `Config#project_name` is the canonical identifier, sourced from the `PROJECT_NAME` env var and used for namespace interpolation (`#{project_name}-#{env}`).

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
