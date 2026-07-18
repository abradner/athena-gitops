#!/bin/bash
set -euo pipefail

# Seeds the 1Password Service Account token that External Secrets Operator
# uses to authenticate. The token is read from the environment so it never
# lands in a file (or a git diff).
#
# Usage:
#   export OP_SERVICE_ACCOUNT_TOKEN="$(op read 'op://<vault>/<item>/credential')"
#   ./load1p-account-token.sh

if [ -z "${OP_SERVICE_ACCOUNT_TOKEN:-}" ]; then
  echo "❌ OP_SERVICE_ACCOUNT_TOKEN is not set."
  echo "   export OP_SERVICE_ACCOUNT_TOKEN=\"\$(op read 'op://<vault>/<item>/credential')\" and re-run."
  exit 1
fi

# Idempotent: safe to re-run during disaster recovery
kubectl create namespace external-secrets --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic op-token -n external-secrets \
  --from-literal=token="$OP_SERVICE_ACCOUNT_TOKEN" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "✅ op-token secret applied to namespace external-secrets."
