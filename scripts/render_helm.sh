#!/usr/bin/env bash
set -eo pipefail

# Renders every Argo CD Application in cluster/ that sources a Helm chart, so
# CI can see what Argo will actually apply.
#
# Why this exists: yq checks that the repo's YAML parses, and kubeconform
# checks that the repo's manifests match Kubernetes schemas. Neither renders a
# chart. An Application is a handful of valid lines that expand into hundreds
# of manifests, and every fault we have had in that expansion was invisible to
# both checks — the values file was valid YAML and the Application was a valid
# Application, while the rendered output was broken.
#
# Usage:
#   scripts/render_helm.sh [OUTDIR]     default OUTDIR: .render
#
# Writes one file per Application: OUTDIR/<app-name>.yaml
# Exit non-zero if any chart fails to render.

OUTDIR="${1:-.render}"
rm -rf "$OUTDIR"
mkdir -p "$OUTDIR"

command -v helm >/dev/null || { echo "❌ helm is required"; exit 1; }
command -v python3 >/dev/null || { echo "❌ python3 is required"; exit 1; }

echo "🔍 Rendering Helm-sourced Argo Applications..."

# Emit one TSV line per Helm source: name, chart, version, repo, values-file.
# Values are written to a temp file rather than passed inline — they routinely
# contain newlines, quotes and templating that do not survive a shell variable.
SPECS=$(mktemp); TMPVALS=$(mktemp -d)
trap 'rm -rf "$SPECS" "$TMPVALS"' EXIT

python3 - "$TMPVALS" "$SPECS" <<'PY'
import os, sys, glob, yaml

tmpvals, specs_out = sys.argv[1], sys.argv[2]
rows = []

def add(name, src, idx):
    chart = src.get("chart")
    if not chart:
        return
    helm = src.get("helm") or {}
    values = helm.get("values")
    if values is None and helm.get("valuesObject") is not None:
        values = yaml.safe_dump(helm["valuesObject"])
    path = os.path.join(tmpvals, f"{name}-{idx}.yaml")
    with open(path, "w") as fh:
        fh.write(values or "")
    rows.append("\t".join([name, chart, str(src.get("targetRevision", "")),
                           str(src.get("repoURL", "")), path]))

for f in glob.glob("cluster/**/*.yaml", recursive=True):
    try:
        docs = list(yaml.safe_load_all(open(f)))
    except Exception:
        continue  # yq's job, not ours
    for d in docs:
        if not isinstance(d, dict) or d.get("kind") != "Application":
            continue
        name = (d.get("metadata") or {}).get("name", "unnamed")
        spec = d.get("spec") or {}
        if spec.get("source"):
            add(name, spec["source"], 0)
        for i, s in enumerate(spec.get("sources") or []):
            add(name, s, i)

open(specs_out, "w").write("\n".join(rows) + ("\n" if rows else ""))
PY

FAILED=0
COUNT=0

while IFS=$'\t' read -r name chart version repo valsfile; do
  [ -z "$name" ] && continue
  COUNT=$((COUNT + 1))
  alias="repo-$(printf '%s' "$repo" | shasum | cut -c1-10)"
  helm repo add "$alias" "$repo" >/dev/null 2>&1 || true

  if out=$(helm template "$name" "$alias/$chart" --version "$version" \
             -f "$valsfile" --namespace default 2>&1); then
    printf '%s\n' "$out" > "$OUTDIR/$name.yaml"
    lines=$(printf '%s\n' "$out" | wc -l | tr -d ' ')
    echo "  ✅ $name ($chart $version) — $lines lines"
  else
    echo "  ❌ $name ($chart $version) failed to render:"
    printf '%s\n' "$out" | sed 's/^/       /' | head -15
    FAILED=$((FAILED + 1))
  fi
done < "$SPECS"

# A silent pass because nothing was found would be worse than a failure — it
# reads identical to "everything rendered fine".
if [ "$COUNT" -eq 0 ]; then
  echo "❌ No Helm-sourced Applications found. Either they moved, or this script"
  echo "   stopped finding them — both need a human, so this is not a pass."
  exit 1
fi

helm repo update >/dev/null 2>&1 || true

if [ "$FAILED" -ne 0 ]; then
  echo "❌ $FAILED of $COUNT chart(s) failed to render."
  exit 1
fi

echo "✅ All $COUNT Helm-sourced Application(s) rendered into $OUTDIR/"
