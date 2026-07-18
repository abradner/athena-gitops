#!/usr/bin/env bash
set -eo pipefail

# Recursively finds and validates all .yaml or .yml files in the repository using yq

echo "🔍 Running syntactic validation on all YAML files..."

if ! command -v yq &> /dev/null; then
  if [ -n "${CI:-}" ]; then
    echo "❌ 'yq' is required in CI — a missing install must fail the check, not skip it."
    exit 1
  fi
  echo "⚠️ 'yq' is not installed! Skipping YAML validation. Install 'yq' to enforce syntactic guarantees."
  exit 0
fi

ERROR_COUNT=0

# Iterate through all YAML files correctly handling spaces/special characters.
# bootstrap/talos is excluded: the templates contain unrendered {{ dotted.key }}
# placeholders, which are not portable YAML (and hydrated files there are
# gitignored anyway).
while IFS= read -r -d '' file; do
  if ! yq eval '.' "$file" > /dev/null 2>&1; then
    echo "❌ Invalid YAML: $file"
    ERROR_COUNT=$((ERROR_COUNT+1))
  fi
done < <(find . -type f \( -name "*.yaml" -o -name "*.yml" \) -not -path "*/\.git/*" -not -path "./bootstrap/talos/*" -print0)

if [ $ERROR_COUNT -ne 0 ]; then
  echo "🚨 Validation failed. Found $ERROR_COUNT invalid YAML file(s)."
  exit 1
fi

echo "✅ All YAML files are syntactically valid."
exit 0
