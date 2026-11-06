#!/usr/bin/env bash
set -eo pipefail

# Regression tests for bootstrap/talos/apply-worker.sh, run in CI.
#
# What they protect: the worker ResolverConfig's literal `domains: []` (the
# DHCP search-domain override, AGENTS.md Gotcha #7) must reach every node it
# is applied to. A 1.13 talosctl's patcher drops empty lists, so the script
# renders every node's config first, checks the rendered files, and only
# then applies them, with no --config-patch at apply time.
#
# The script runs for real against the real worker template, with its
# {{ placeholders }} stubbed, and a fake `talosctl`:
#   - `machineconfig patch` copies the base to --output and records the
#     patches it was given in a comment. With FAKE_DROP_EMPTY=1 it also
#     drops `domains: []`, as the real 1.13 client does (reproduced with
#     1.13.4; 1.14.4 keeps it).
#   - `apply-config` records what it would have put on the node.
# Needs bash and mikefarah yq 4. Contacts nothing.

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

mkdir -p "$work/bin"
cat > "$work/bin/talosctl" <<'EOF'
#!/usr/bin/env bash
if [ "$1" = machineconfig ] && [ "$2" = patch ]; then
  base="$3"; shift 3; out=""; patches=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --patch|-p) patches="$patches $2"; shift ;;
      --output|-o) out="$2"; shift ;;
    esac
    shift
  done
  [ -n "$out" ] || { echo "fake talosctl: no --output" >&2; exit 1; }
  { echo "# fake-patches:$patches"; cat "$base"; } > "$out"
  dirname "$out" >> "$APPLY_LOG.renders"
  if [ "${FAKE_DROP_EMPTY:-0}" = 1 ]; then
    grep -v '^    domains: \[\]$' "$out" > "$out.tmp" && cat "$out.tmp" > "$out" && rm -f "$out.tmp"
  fi
  exit 0
fi
if [ "$1" = apply-config ]; then
  file=""; patched=no; node=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --file|-f) file="$2"; shift ;;
      --nodes|-n) node="$2"; shift ;;
      --config-patch|-p) patched=yes; shift ;;
    esac
    shift
  done
  patches="$(sed -n 's/^# fake-patches: //p' "$file")"
  domains="$(yq -o=json -I=0 'select(.kind == "ResolverConfig") | .searchDomains.domains' "$file")"
  mode="$(stat -c %a "$file" 2>/dev/null || stat -f %Lp "$file")"
  echo "node=$node patched=$patched domains=$domains mode=$mode patches=$patches" >> "$APPLY_LOG"
  exit 0
fi
echo "fake talosctl: unexpected call: $*" >&2
exit 1
EOF
chmod +x "$work/bin/talosctl"

# setup <athena.zsh body>: a fresh bootstrap/ tree whose worker.yaml is the
# real template with placeholders stubbed.
setup() {
  rm -rf "$work/bootstrap"
  mkdir -p "$work/bootstrap/talos"
  printf '%s\n' "$1" > "$work/bootstrap/athena.zsh"
  cp "$script" "$work/bootstrap/talos/apply-worker.sh"
  sed -E 's/\{\{[^}]*\}\}/stub/g' "$repo/bootstrap/talos/worker.template.yaml" > "$work/bootstrap/talos/worker.yaml"
  : > "$work/apply.log"
  : > "$work/apply.log.renders"
}

# run: execute the script; sets $status and $output.
run() {
  set +e
  output="$(cd "$work/bootstrap/talos" && APPLY_LOG="$work/apply.log" PATH="$work/bin:$PATH" bash apply-worker.sh 2>&1)"
  status=$?
  set -e
}

applies() { grep -c . "$work/apply.log" || true; }
logline() { grep -c -- "$1" "$work/apply.log" || true; }

all_declared='GPU_WORKER_IP=(); GPU_VM_WORKER_IP=()'

echo "🔍 apply-worker.sh regression tests"

# The template itself must carry the override, or every other test is moot.
echo "• the worker template's ResolverConfig has a literal domains: []"
if [ "$(sed -E 's/\{\{[^}]*\}\}/stub/g' "$repo/bootstrap/talos/worker.template.yaml" \
  | yq -o=json -I=0 'select(.kind == "ResolverConfig") | .searchDomains.domains')" = "[]" ]; then
  pass "template carries it"
else
  fail "worker.template.yaml has no ResolverConfig with domains: []"
fi

echo "• every class: rendered file applied, domains: [], 0600, right patches, no --config-patch"
setup 'WORKER_IP=("w1"); NVME_WORKER_IP=("n1" "n2"); GPU_WORKER_IP=("g1:9.5"); GPU_VM_WORKER_IP=("v1:11")'
run
if [ "$status" -eq 0 ] && [ "$(applies)" -eq 5 ] \
  && [ "$(logline 'patched=no domains=\[\] mode=600 ')" -eq 5 ] \
  && [ "$(logline '^node=w1 .* patches=@watchdog.patch.yaml$')" -eq 1 ] \
  && [ "$(logline '^node=n[12] .* patches=@worker-nvme.patch.yaml @watchdog.patch.yaml$')" -eq 2 ] \
  && [ "$(logline '^node=g1 .* patches=@worker-gpu.patch.yaml @watchdog.patch.yaml {"machine":{"nodeLabels":{"spritz.dev/gpu-gen":"9.5"}}}$')" -eq 1 ] \
  && [ "$(logline '^node=v1 .* patches=@worker-gpu-vm.patch.yaml @watchdog.patch.yaml {"machine":{"nodeLabels":{"spritz.dev/gpu-gen":"11"}}}$')" -eq 1 ]; then
  pass "1 base + 2 scratch + 1 GPU + 1 GPU VM applied correctly"
else
  fail "normal run: status=$status"; cat "$work/apply.log" >&2; echo "$output" >&2
fi

echo "• rendered configs (cluster PKI) are removed on exit"
left=0
while read -r d; do [ -e "$d" ] && left=$((left + 1)); done < <(sort -u "$work/apply.log.renders")
if [ "$(grep -c . "$work/apply.log.renders" || true)" -gt 0 ] && [ "$left" -eq 0 ]; then
  pass "render directory gone"
else
  fail "render directory survived (or nothing was rendered): left=$left"
fi

echo "• no scratch or GPU nodes: base only"
setup "WORKER_IP=(\"w1\"); NVME_WORKER_IP=(); $all_declared"
run
if [ "$status" -eq 0 ] && [ "$(applies)" -eq 1 ]; then pass "base only"; else fail "base-only run: status=$status applies=$(applies)"; echo "$output" >&2; fi

echo "• NVME_WORKER_IP undeclared: refuse"
setup "WORKER_IP=(\"w1\"); $all_declared"
run
if [ "$status" -ne 0 ] && [ "$(applies)" -eq 0 ]; then pass "refused, nothing applied"; else fail "undeclared: status=$status applies=$(applies)"; fi

echo "• a patcher that drops domains: [] (the 1.13 client) → refuse BEFORE touching any node"
setup "WORKER_IP=(\"w1\" \"w2\"); NVME_WORKER_IP=(\"n1\"); $all_declared"
FAKE_DROP_EMPTY=1 run
if [ "$status" -ne 0 ] && [ "$(applies)" -eq 0 ] && grep -q '1.14 client' <<<"$output"; then
  pass "refused, nothing applied, names the client"
else
  fail "dropping patcher: status=$status applies=$(applies)"; cat "$work/apply.log" >&2; echo "$output" >&2
fi

echo "• only the last class loses domains: [] → still nothing applied to the earlier ones"
setup "WORKER_IP=(\"w1\"); NVME_WORKER_IP=(\"n1\"); GPU_WORKER_IP=(); GPU_VM_WORKER_IP=(\"v1:11\")"
# Wrap the fake so only the GPU VM render drops the list.
mv "$work/bin/talosctl" "$work/bin/talosctl.real"
cat > "$work/bin/talosctl" <<'EOF'
#!/usr/bin/env bash
case "$*" in *worker-gpu-vm.patch.yaml*) export FAKE_DROP_EMPTY=1 ;; esac
exec "$(dirname "$0")/talosctl.real" "$@"
EOF
chmod +x "$work/bin/talosctl"
run
mv "$work/bin/talosctl.real" "$work/bin/talosctl"
if [ "$status" -ne 0 ] && [ "$(applies)" -eq 0 ]; then pass "refused, nothing applied"; else fail "late failure: status=$status applies=$(applies)"; cat "$work/apply.log" >&2; fi

echo "• base config without domains: [] → refuse BEFORE touching any node"
setup "WORKER_IP=(\"w1\" \"w2\"); NVME_WORKER_IP=(\"n1\"); $all_declared"
grep -v '^    domains: \[\]$' "$work/bootstrap/talos/worker.yaml" > "$work/w.tmp" && mv "$work/w.tmp" "$work/bootstrap/talos/worker.yaml"
run
if [ "$status" -ne 0 ] && [ "$(applies)" -eq 0 ]; then pass "refused, nothing applied"; else fail "missing domains: status=$status applies=$(applies)"; cat "$work/apply.log" >&2; fi

echo "• malformed GPU entry → refuse BEFORE touching any node"
setup 'WORKER_IP=("w1"); NVME_WORKER_IP=(); GPU_WORKER_IP=("g1"); GPU_VM_WORKER_IP=()'
run
if [ "$status" -ne 0 ] && [ "$(applies)" -eq 0 ]; then pass "refused, nothing applied"; else fail "malformed GPU: status=$status applies=$(applies)"; fi

if [ "$failures" -gt 0 ]; then
  echo "❌ $failures failure(s)." >&2
  exit 1
fi
echo "✅ All apply-worker.sh tests passed."
