#!/bin/bash
# Install the CNI and Argo CD on zagreus, the CI build cluster, after the
# control plane has bootstrapped. See bootstrap/zagreus/README.md for where
# this sits in the order of operations.
#
# Run with KUBECONFIG pointing at zagreus. It refuses to run against any other
# cluster: this repo's other provision.sh targets athena, and installing
# zagreus's Argo values there would move athena's Argo onto its control plane.
set -euo pipefail
cd "$(dirname "$0")"

# Keep in step with athena's bootstrap/kubernetes/provision.sh.
CILIUM_VERSION="1.19.4"
ARGOCD_CHART_VERSION="9.7.1"

context="$(kubectl config current-context)"
case "$context" in
  *zagreus*) ;;
  *) echo "Current kube context is '$context', not zagreus. Refusing." >&2; exit 1 ;;
esac

helm upgrade --install cilium oci://quay.io/cilium/charts/cilium \
  --version "$CILIUM_VERSION" \
  --namespace kube-system \
  -f cilium-values.yaml

helm repo add argo https://argoproj.github.io/argo-helm --force-update
helm upgrade --install argocd argo/argo-cd \
  --version "$ARGOCD_CHART_VERSION" \
  --namespace argocd --create-namespace \
  -f argocd-values.yaml

kubectl rollout status deploy argocd-repo-server -n argocd --timeout=10m
echo "Argo CD is up. Next: load the 1Password token, then apply cluster/zagreus/root.yaml."
