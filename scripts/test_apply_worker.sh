#!/usr/bin/env bash
set -eo pipefail

# Regression tests for bootstrap/talos/apply-worker.sh, run in CI.
#
# What they protect: the worker ResolverConfig's literal `domains: []` (the
# DHCP search-domain override, AGENTS.md Gotchas #5) must reach every node it
# is applied to. It is lost silently if scratch configs go back through a 1.13
# talosctl's --config-patch, which drops empty lists, or if a bad config gets
# applied before the preflight notices.
#
# The script runs for real against the real worker template, with its
# {{ placeholders }} stubbed, and a fake `talosctl` that records what it would
# have applied. Needs bash and mikefarah yq 4.

if ! command -v yq &> /dev/null; then
  echo "❌ 'yq' is required for these tests." >&2
  exit 1
fi

repo="$(cd "$(dirname "$0")/.." && pwd)"
# APPLY_WORKER lets a reviewer point the suite at another version of the
# script (e.g. main's) to watch it fail.
script="${APPLY_WORKER:-$repo/bootstrap/talos/apply-worker.sh}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

failures=0
pass() { echo "  ✅ $1"; }
fail() { echo "  ❌ $1" >&2; failures=$((failures + 1)); }

# Fake talosctl: one line per apply, naming the file it was given, whether a
# --config-patch was used, and what that file would have put on the node.
mkdir -p "$work/bin"
cat > "$work/bin/talosctl" <<'EOF'
#!/usr/bin/env bash
file=""; patched=no; node=""
while [ $# -gt 0 ]; do
  case "$1" in
    --file) file="$2"; shift ;;
    --nodes) node="$2"; shift ;;
    --config-patch) patched=yes; shift ;;
  esac
  shift
done
# No `// "default"` here: yq 4.44 (CI's pin) applies the alternative to
# documents the select() already filtered out, printing one line per doc.
label="$(yq 'select(has("machine")) | .machine.nodeLabels."athena.asn.casa/scratch"' "$file")"
domains="$(yq -o=json -I=0 'select(.kind == "ResolverConfig") | .searchDomains.domains' "$file")"
mode="$(stat -c %a "$file")"
echo "node=$node patched=$patched label=$label domains=$domains mode=$mode" >> "$APPLY_LOG"
EOF
chmod +x "$work/bin/talosctl"

# setup <athena.zsh body>: a fresh bootstrap/ tree whose worker.yaml is the
# real template with placeholders stubbed.
setup() {
  rm -rf "$work/bootstrap"
  mkdir -p "$work/bootstrap/talos"
  printf '%s\n' "$1" > "$work/bootstrap/athena.zsh"
  cp "$script" "$work/bootstrap/talos/apply-worker.sh"
  cp "$repo/bootstrap/talos/worker-nvme.patch.yaml" "$work/bootstrap/talos/"
  sed -E 's/\{\{[^}]*\}\}/stub/g' "$repo/bootstrap/talos/worker.template.yaml" > "$work/bootstrap/talos/worker.yaml"
  : > "$work/apply.log"
}

# run: execute the script; sets $status and $output.
run() {
  set +e
  output="$(cd "$work/bootstrap/talos" && APPLY_LOG="$work/apply.log" PATH="$work/bin:$PATH" bash apply-worker.sh 2>&1)"
  status=$?
  set -e
}

applies() { grep -c . "$work/apply.log" || true; }

echo "🔍 apply-worker.sh regression tests"

echo "• scratch nodes get the label and keep domains: [] (no --config-patch)"
setup 'WORKER_IP=("w1"); NVME_WORKER_IP=("n1" "n2")'
run
if [ "$status" -eq 0 ] && [ "$(applies)" -eq 3 ] \
  && grep -q '^node=w1 patched=no label=null domains=\[\]' "$work/apply.log" \
  && [ "$(grep -c '^node=n[12] patched=no label=nvme domains=\[\] mode=600$' "$work/apply.log")" -eq 2 ]; then
  pass "1 base + 2 scratch applied correctly"
else
  fail "normal run: status=$status"; cat "$work/apply.log" >&2; echo "$output" >&2
fi

echo "• no scratch nodes: base only"
setup 'WORKER_IP=("w1"); NVME_WORKER_IP=()'
run
if [ "$status" -eq 0 ] && [ "$(applies)" -eq 1 ]; then pass "base only"; else fail "no-scratch run: status=$status applies=$(applies)"; fi

echo "• NVME_WORKER_IP undeclared: refuse"
setup 'WORKER_IP=("w1")'
run
if [ "$status" -ne 0 ] && [ "$(applies)" -eq 0 ]; then pass "refused, nothing applied"; else fail "undeclared: status=$status applies=$(applies)"; fi

echo "• base config without domains: [] → refuse BEFORE touching any node"
setup 'WORKER_IP=("w1" "w2"); NVME_WORKER_IP=("n1")'
sed -i '/^    domains: \[\]$/d' "$work/bootstrap/talos/worker.yaml"
run
if [ "$status" -ne 0 ] && [ "$(applies)" -eq 0 ]; then pass "refused, nothing applied"; else fail "missing domains: status=$status applies=$(applies)"; cat "$work/apply.log" >&2; fi

if [ "$failures" -gt 0 ]; then
  echo "❌ $failures failure(s)." >&2
  exit 1
fi
echo "✅ All apply-worker.sh tests passed."
