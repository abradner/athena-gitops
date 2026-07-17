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
| `cluster/apps/` | Workload sync targets: the `pmn` ApplicationSet (per-service, per-environment apps from `pmn-workloads`) and repo credentials. |
| `root-app.yaml` | The app-of-apps. Applied once during bootstrap; recursively syncs everything under `cluster/`. |
| `scripts/validate_yaml.sh` | Syntactic YAML validation (yq), run by the pre-commit hook and CI. |

## How syncing works

`root-app.yaml` points Argo CD at `cluster/` with `directory.recurse: true`,
automated sync, prune, and self-heal enabled. **Anything merged to `main`
applies to the cluster automatically** — including deletions. Sync-wave
annotations order CRD-dependent resources (e.g. the `ClusterSecretStore`
waits for the External Secrets Operator).

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
