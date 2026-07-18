# Athena GitOps

Cluster management repository for the Athena homelab: a bare-metal Kubernetes
cluster on [Talos](https://www.talos.dev/), reconciled by
[Argo CD](https://argo-cd.readthedocs.io/) with secrets sourced from 1Password
via [External Secrets Operator](https://external-secrets.io/).

## Layout

| Path | Purpose |
| --- | --- |
| `bootstrap/` | Day-0 provisioning and the [disaster-recovery playbook](bootstrap/README.md): Talos config templates, Cilium/Gateway API/Argo CD install scripts. |
| `cluster/core/` | Foundational cluster services, synced by Argo CD: External Secrets Operator + 1Password `ClusterSecretStore`, metrics-server, Headlamp. |
| `cluster/apps/` | Workload sync targets: the `pmn` ApplicationSet (per-service, per-environment apps from `pmn-workloads`), repo credentials, and the hello-world canary. |
| `cluster/root.yaml` | The app-of-apps root. Applied once during bootstrap, self-managing afterwards; syncs only the top level of `cluster/` (AppProjects + the two child apps below). |
| `cluster/cluster-core.yaml`, `cluster/cluster-apps.yaml` | The two child Applications that own `cluster/core` and `cluster/apps` respectively. |
| `cluster/projects.yaml` | AppProjects: `core` (this repo + pinned chart repos, infra namespaces) and `pmn` (`pmn-workloads` → `pmn-*` namespaces only). |
| `scripts/validate_yaml.sh` | Syntactic YAML validation (yq), run by the pre-commit hook and CI. |

## How syncing works

`cluster/root.yaml` syncs the top level of `cluster/` (non-recursive,
prune off): the AppProjects and two child Applications. `cluster-core` and
`cluster-apps` then recursively sync their directories with automated
prune and self-heal. **Anything merged to `main` applies to the cluster
automatically** — including deletions inside `core/` and `apps/`. Sync-wave
annotations order CRD-dependent resources (e.g. the `ClusterSecretStore`
waits for the External Secrets Operator), and Applications are scoped to
the `core`/`pmn` AppProjects to bound what each source repo can deploy
and where.

## Validation

- Local: a pre-commit hook runs `scripts/validate_yaml.sh` (install `yq` via
  `mise install`).
- CI: `.github/workflows/validate.yaml` runs the same syntax check plus
  [kubeconform](https://github.com/yannh/kubeconform) schema validation on
  every push and pull request.

## Related repositories

- [`pmn-workloads`](https://github.com/abradner/pmn-workloads) — workload
  manifests synced by the `pmn` ApplicationSet.
- `tools-workflow` — automation pipeline (Talos config hydration from
  1Password, Argo manifest generation).

Agent onboarding notes live in [AGENTS.md](AGENTS.md).
