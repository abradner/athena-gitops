#!/bin/bash
set -euo pipefail

# Pinned versions — bump deliberately, and keep in sync with what the cluster
# is actually running so a disaster-recovery rebuild reproduces the same state.
GATEWAY_API_VERSION="v1.2.1" # later versions break cilium 1.19 due to naming changes
CILIUM_VERSION="1.19.4"
# Keep in sync with cluster/core/argocd/argocd-app.yaml — that Application
# adopts this release once the root app syncs. Renovate now keeps the two in
# step via a customManager (see renovate.json5); they are grouped into one PR
# so this line cannot be left behind again.
ARGOCD_CHART_VERSION="9.7.1"

# Install gateway-api

kubectl apply -f "https://github.com/kubernetes-sigs/gateway-api/releases/download/${GATEWAY_API_VERSION}/standard-install.yaml"
kubectl apply -f "https://github.com/kubernetes-sigs/gateway-api/releases/download/${GATEWAY_API_VERSION}/experimental-install.yaml"

# Install helm (skip if already present)

if ! command -v helm &> /dev/null; then
  sudo apt-get install curl gpg apt-transport-https --yes
  curl -fsSL https://packages.buildkite.com/helm-linux/helm-debian/gpgkey | gpg --dearmor | sudo tee /usr/share/keyrings/helm.gpg > /dev/null
  echo "deb [signed-by=/usr/share/keyrings/helm.gpg] https://packages.buildkite.com/helm-linux/helm-debian/any/ any main" | sudo tee /etc/apt/sources.list.d/helm-stable-debian.list
  sudo apt-get update
  sudo apt-get install helm --yes
fi

# Cilium — values live in cilium-values.yaml so CNI config is reviewable and reproducible

helm upgrade --install cilium oci://quay.io/cilium/charts/cilium \
  --version "$CILIUM_VERSION" \
  --namespace kube-system \
  -f cilium-values.yaml

# Create a gateway
kubectl apply -f gateway-homelab-pool.yaml

# Install argo via the Helm chart (idempotent: safe to re-run). The release
# is adopted by cluster/core/argocd/argocd-app.yaml after the root app is
# applied, making later upgrades declarative.
helm repo add argo https://argoproj.github.io/argo-helm --force-update
helm upgrade --install argocd argo/argo-cd \
  --version "$ARGOCD_CHART_VERSION" \
  --namespace argocd --create-namespace \
  -f argocd-values.yaml

# Add it to the gateway
kubectl apply -f argocd-gateway.yaml
kubectl apply -f hubble-gateway.yaml

# verify and get the initial admin password

kubectl rollout status deploy argocd-server -n argocd --timeout=5m
kubectl get gateway homelab-gateway

# The initial admin secret is generated on argocd-server's first boot and can
# lag the rollout. Poll for it, but never let the display step fail the script
# (set -e) — on re-runs the secret may already have been rotated or deleted.
echo "ArgoCD Admin Password:"
for _ in $(seq 1 30); do
  if password="$(kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' 2>/dev/null | base64 -d)" && [ -n "$password" ]; then
    echo "$password"
    exit 0
  fi
  sleep 5
done
echo "⚠️ argocd-initial-admin-secret not available (server still starting, or secret already rotated/deleted)."
echo "   Retry with: kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d"
