#!/bin/bash
set -euo pipefail

# Pinned versions — bump deliberately, and keep in sync with what the cluster
# is actually running so a disaster-recovery rebuild reproduces the same state.
GATEWAY_API_VERSION="v1.2.1" # later versions break cilium 1.19 due to naming changes
CILIUM_VERSION="1.19.4"
ARGOCD_VERSION="${ARGOCD_VERSION:-stable}" # pin a tag (e.g. v3.1.0) for reproducible DR

# Install gateway-api

kubectl apply -f "https://github.com/kubernetes-sigs/gateway-api/releases/download/${GATEWAY_API_VERSION}/standard-install.yaml"
kubectl apply -f "https://github.com/kubernetes-sigs/gateway-api/releases/download/${GATEWAY_API_VERSION}/experimental-install.yaml"

# Install helm (skip if already present)

if ! command -v helm &> /dev/null; then
  sudo apt-get install curl gpg apt-transport-https --yes
  curl -fsSL https://packages.buildkite.com/helm-linux/helm-debian/gpgkey | gpg --dearmor | sudo tee /usr/share/keyrings/helm.gpg > /dev/null
  echo "deb [signed-by=/usr/share/keyrings/helm.gpg] https://packages.buildkite.com/helm-linux/helm-debian/any/ any main" | sudo tee /etc/apt/sources.list.d/helm-stable-debian.list
  sudo apt-get update
  sudo apt-get install helm
fi

# Cilium — values live in cilium-values.yaml so CNI config is reviewable and reproducible

helm upgrade --install cilium oci://quay.io/cilium/charts/cilium \
  --version "$CILIUM_VERSION" \
  --namespace kube-system \
  -f cilium-values.yaml

# Create a gateway
kubectl apply -f gateway-homelab-pool.yaml

# Install argo (idempotent: safe to re-run)
kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -
kubectl apply -n argocd --server-side --force-conflicts -f "https://raw.githubusercontent.com/argoproj/argo-cd/${ARGOCD_VERSION}/manifests/install.yaml"
kubectl patch configmap argocd-cmd-params-cm -n argocd --type merge -p '{"data":{"server.insecure":"true"}}'
kubectl rollout restart deploy argocd-server -n argocd

# Add it to the gateway
kubectl apply -f argocd-gateway.yaml
kubectl apply -f hubble-gateway.yaml

# verify and get the initial admin password

kubectl get gateway homelab-gateway
echo "ArgoCD Admin Password:"
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d
