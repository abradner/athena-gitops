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

### `cluster/apps/`
Overarching ArgoCD `Application` custom resources. These establish sync targets linking ArgoCD to upstream workload repositories (e.g., `pmn-workloads`).

### `cluster/core/`
Foundational cluster integrations:
- `onepassword-backend` `ClusterSecretStore` for ExternalSecrets retrieval via 1Password.
- ArgoCD GitHub repository authentication via an `ExternalSecret` (`argocd-github-repo-pmn.yaml`) that maps a GitHub PAT from 1Password into the ArgoCD control plane.

## Agent Guidelines & Operation
- **Argo App Instantiation**: The `GenerateArgocd` orchestrator in `tools-workflow` generates `cluster/apps/` manifests, establishing sync targets per application and environment.
- **Syntactic Validation Only**: This repository uses syntactical YAML verification. Do not attempt behavioural spec logic. A `.git/hooks/pre-commit` hook uses `yq` (via `mise.toml`) to run `./scripts/validate_yaml.sh`, preventing structurally invalid commits.
- **Do Not Hardcode Secrets**: All sensitive values must be templated and sourced from 1Password. If you encounter plaintext certs or tokens in committed files, flag them immediately.

## Recent Snapshot Learnings & Insights
1. **1Password API Rate Limiting**: High workload counts can cause ExternalSecret refresh cycles to exceed 1Password's rate limits. `refreshInterval` has been scaled to `24h` / `6h` cycles to reduce authentication pressure.
2. **Application-Scoped Resources**: Centralising shared resources (e.g., `registry-pull-secret`) into a singular infra app disrupts ArgoCD sync dependencies. Resources are scoped per-application (e.g., `pmn-ext-gw-registry`) to maintain clean ownership topologies.
3. **Cluster-Level Networking**: External hostnames for cluster-level services (such as ArgoCD and Hubble UI) use the `*.athena.asn.casa` domain. These hostnames are typically secured via local HAProxy configurations to remain visible only on the local LAN.
