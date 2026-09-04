#!/usr/bin/env bash
# Size and classify a commit range for a quality audit.
#   scope.sh <base> <target> [label] [out-dir]
# Prints the facts that decide how to slice the fleet, and writes
# <out-dir>/commits-<label>.txt with bot bumps (Dependabot/Renovate/deps
# chores) excluded — the file every digger and assessor is pointed at.
set -euo pipefail
base=${1:?base ref}; target=${2:?target ref}; label=${3:-range}; out=${4:-.}
[[ "$label" =~ ^[A-Za-z0-9_.-]+$ ]] || { echo "fatal: label must be letters, digits, '_', '.', '-' only — got '$label'" >&2; exit 1; }
range="$base..$target"
mkdir -p "$out"

# Anchored to the true start of the line, with the hash prefix optional (not "start, OR a
# hash anywhere") — an earlier version of this fix used `(^|[0-9a-f]{4,40} )`, which still let
# the hex-and-space branch match mid-subject: "abc1234 feat: mention deadbeef renovate cache"
# has no bot-bump hash of its own, but "deadbeef " looks like one wherever it appears.
# dependabot/renovate need a delimiter after them too, not just a start anchor before: without
# one, a real untyped commit like "Renovated the audit dashboard" matches "renovate" as a plain
# prefix (case-insensitive) and is silently misclassified as a bot bump. Bot commits in this
# fleet always read "dependabot: ..." / "renovate: ...", so requiring ':' or end-of-line covers
# every real bot commit while excluding a real subject that merely starts with the same letters.
bots='^([0-9a-f]{4,40} )?((chore|fix)\(deps(-dev)?\)|(dependabot|renovate)(:|$))'  # optional hash prefix from --oneline, then the true line start
# `|| true` on the pipeline as a whole would mask a real git-log failure (bad range) behind
# grep's expected "no bot-free lines" exit 1 — under pipefail either failure looks the same.
# Check git log on its own first; only grep's no-match is tolerated.
if ! log_out=$(git log --oneline --no-decorate --no-merges "$range"); then
  echo "fatal: git log failed for range '$range' — check base/target refs" >&2
  exit 1
fi
# Same reasoning as the git-log check above, one level down: grep's own failure (a bad
# regex, not just zero matches) must not read the same as "nothing to exclude."
# printf, not echo: echo emits a blank line even for a truly-empty $log_out (a merge-only
# or zero-commit range), and grep -v keeps that line — reporting one audited commit that
# doesn't exist. printf leaves empty input empty.
printf '%s' "$log_out" | grep -viE "$bots" > "$out/commits-$label.txt" || {
  rc=$?
  [ "$rc" -eq 1 ] || { echo "fatal: grep failed classifying commits for range '$range' (exit $rc)" >&2; exit 1; }
}

total=$(git log --oneline --no-decorate "$range" | wc -l | tr -d ' ')
merges=$(git log --oneline --no-decorate --merges "$range" | wc -l | tr -d ' ')
real=$(wc -l < "$out/commits-$label.txt" | tr -d ' ')
echo "== RANGE $range ($label) =="
echo "commits: $total  merges: $merges  bot-bumps: $((total - merges - real))  audited: $real"
git diff --shortstat "$range"

echo; echo "== DIRECTORY FOOTPRINT (top 15) =="
# awk 'NR<=15' rather than `head -15`: head closes its read end once satisfied, and on a
# large footprint the upstream `sort -rn` can still be mid-write when that happens — under
# pipefail the SIGPIPE it takes aborts the whole script. awk drains the pipe to EOF instead.
git diff --name-only "$range" | awk -F/ '{if (NF>2) print $1"/"$2; else if (NF>1) print $1; else print "(root)"}' \
  | sort | uniq -c | sort -rn | awk 'NR<=15'

echo; echo "== MIGRATIONS =="
# Tries common conventions; add your own directory to the list below if this repo uses one.
migrate_dirs="db/migrate migrations"
mig_found=0
for d in $migrate_dirs; do
  [ -d "$d" ] || continue
  added=$(git diff --name-only --diff-filter=A "$range" -- "$d")
  [ -n "$added" ] && { echo "$added" | sed "s|^|  |"; mig_found=1; }
done
[ "$mig_found" -eq 0 ] && echo "  (none)"

echo; echo "== NEW RAKE TASKS / BIN SCRIPTS =="
new_scripts=$(git diff --name-only --diff-filter=A "$range" -- lib/tasks bin)
if [ -n "$new_scripts" ]; then
  echo "$new_scripts" | sed 's|^|  |'
else
  echo "  (none)"
fi

echo; echo "== PAYLOAD / CONTRACT VERSION BUMPS =="
# Optional, repo-specific: list the files that carry a schema/contract/payload version constant,
# one per line, in a sibling `contract-paths.txt` next to this script. Skipped if absent or empty
# — an unset list must never fall through to `git diff` with no path restriction.
contract_paths="$(dirname "$0")/contract-paths.txt"
if [ -s "$contract_paths" ]; then
  paths=()
  while IFS= read -r p; do [ -n "$p" ] && paths+=("$p"); done < "$contract_paths"
  if [ "${#paths[@]}" -gt 0 ]; then
    if diff_out=$(git diff "$range" -- "${paths[@]}" 2>&1); then
      echo "$diff_out" | grep -E '^[+-].*(SCHEMA_VERSION|CONTRACT_VERSION|"version")' || echo "  (none)"
    else
      echo "  (git diff failed — check contract-paths.txt entries and the range: $diff_out)"
    fi
  else
    echo "  (skipped — contract-paths.txt has no non-blank lines)"
  fi
else
  echo "  (skipped — no contract-paths.txt; see SKILL.md 'Establish before first use')"
fi

echo; echo "== FEATURE THEMES (commit scopes, by frequency) =="
# Two sed invocations, not one: the always-succeeding hash-strip below would set the
# t-flag before the classification checks even run, making the first t branch on the
# hash-strip's own success rather than the paren-pattern match — every line would then
# skip straight to keeping its post-strip text, never reaching "(untyped)".
# awk 'NR<=12' rather than `head -12`, same SIGPIPE-under-pipefail reasoning as the
# directory footprint above: it drains `sort -rn` to EOF instead of closing early on it.
sed -E 's/^[0-9a-f]+ //' "$out/commits-$label.txt" \
  | sed -E 's/^([[:alnum:]_]+)\(([^)]+)\).*/\2/; t; s/^([[:alnum:]_]+):.*/\1/; t; s/.*/(untyped)/' \
  | sort | uniq -c | sort -rn | awk 'NR<=12'

echo; echo "== REVIEW-FEEDBACK BATCHES (a lot of first-pass findings are already fixed here) =="
grep -iE 'review feedback|feedback batch|self-review|UAT round' "$out/commits-$label.txt" || echo "  (none)"

echo; echo "wrote $out/commits-$label.txt"
