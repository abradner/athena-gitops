#!/bin/bash
# Checks the DaemonSet health override in argocd.yaml (configs.cm) with
# argocd's own Lua runner. Run from the repo root: needs ruby and the argocd CLI.

set -u
T=$(mktemp -d)
ruby -ryaml -e 'd=YAML.load_file("cluster/zagreus/core/argocd.yaml"); cm={"apiVersion"=>"v1","kind"=>"ConfigMap","metadata"=>{"name"=>"argocd-cm","namespace"=>"argocd"},"data"=>d.dig("spec","source","helm","valuesObject","configs","cm")}; File.write(ARGV[0], cm.to_yaml)' "$T/cm.yaml"
mkds() { cat > "$T/$1.yaml" <<EOF
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: t
  namespace: x
  generation: $2
  annotations: {$3}
spec:
  updateStrategy: {type: RollingUpdate}
status:
  observedGeneration: $4
  desiredNumberScheduled: 9
  updatedNumberScheduled: $5
  numberAvailable: $6
EOF
}
mkds annotated_down 3 'zagreus.asn.casa/health: rollout-observed' 3 8 7
mkds plain_down 3 '' 3 9 7
mkds plain_up 3 '' 3 9 9
mkds annotated_unobserved 4 'zagreus.asn.casa/health: rollout-observed' 3 9 9
mkds plain_updating 3 '' 3 6 9
fail=0
for pair in annotated_down:Healthy plain_down:Progressing plain_up:Healthy annotated_unobserved:Progressing plain_updating:Progressing; do
  c=${pair%%:*} want=${pair#*:}
  got=$(argocd admin settings resource-overrides health "$T/$c.yaml" --argocd-cm-path "$T/cm.yaml" 2>/dev/null | awk '/^STATUS:/ {print $2}')
  if [ "$got" = "$want" ]; then echo "ok   $c: $got"; else echo "FAIL $c: got '$got', want $want"; fail=1; fi
done
rm -rf "$T"
exit $fail
