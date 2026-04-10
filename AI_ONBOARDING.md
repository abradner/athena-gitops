# Antigravity & Gemini Agent Onboarding: Athena GitOps

## Repository Overview
`athena-gitops` acts as the fundamental cluster management and foundational control plane repository. It maintains raw Kubernetes bootstrap assets alongside ArgoCD management scopes globally overseeing Application configuration targeting repositories successfully.

## Domain Nomenclature
- **Application**: Refers strictly to individual microservices (e.g., `pmn-ext-gw`) and maps identically to the ArgoCD Application schema.
- **Project**: Represents the overall global suite deployment scope.

## File Layout & Separation of Concerns
- **`bootstrap/`**: Raw underlying OS/Cluster logic arrays (node provisions, Talos configurations) and explicit physical server assignments dynamically managing the clusters.
- **`cluster/apps/`**: Stores overarching `Application` custom resources driving ArgoCD into the targeted source repositories (`pmn-workloads`, etc).
- **Global Suite Linkage**: The `cluster/apps/` orchestrations directly link the upstream `pmn-workloads` definitions via dynamic `Project` interpolation globally.
- **Core Infrastructure Targets**: Explicit foundational integrations globally managed inside the cluster scopes natively, including:
  - `onepassword-backend` `ClusterSecretStore` integration handling ExternalSecrets retrieval.

## Agent Guidelines & Operation
- **Argo App Instantiation Pipeline**: The `GenerateArgocd` orchestration scripts heavily rely upon configuring `cluster/apps/` directly downstream establishing explicit sync targets matching environments properly globally.
- **Test Integrity (Syntactic Validation)**: This repository relies purely on syntactical verification of Kubernetes YAML. Do not attempt to run behavioral spec logic here. Always execute `./scripts/validate_yaml.sh` locally to validate structure recursively via `yq` before committing.

## Recent Snapshot Learnings & Insights
1. **API Rate Limiting Dynamics**: Application scaling effectively multiplied ExternalSecret retrieval locks overlapping natively upon 1Password endpoints natively across Argo scopes triggering `"rate limit exceeded"` HTTP locks. ExternalSecret base refresh parameters explicitly scaled backwards dynamically onto `24h` and `6h` cycles cleanly alleviating continuous authentication pressure at the top operator levels.
2. **Coupling Resolution**: When attempting to centralize `registry-pull-secret` logically outside the isolated workloads explicitly into a singular `infra` target app, it natively disrupts sync dependencies internally requiring external apps to hold-over. Purely decoupling these assets directly per-app (`test-app-registry`) maintains pristine topological application trees and is structurally preferred across deployments locally. 
