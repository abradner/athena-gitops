#!/usr/bin/env bash
# Regression test for scope.sh's bot-bump filter, its feature-theme classification,
# and the two scope.sh copies staying identical.
# Run: .claude/skills/quality-audit/test-scope.sh
set -euo pipefail
cd "$(dirname "$0")/../../.."

fail=0

# Both copies of scope.sh are meant to be byte-identical (quality-audit and
# quality-audit-refresh use the same script). A drift here means one was edited
# without the other and the two skills silently disagree on scoping.
if ! diff -q .claude/skills/quality-audit/scope.sh .claude/skills/quality-audit-refresh/scope.sh >/dev/null; then
  echo "FAIL: quality-audit/scope.sh and quality-audit-refresh/scope.sh have diverged"
  fail=1
fi

# Extract just the bots pattern from the script under test, so this test exercises
# the actual shipped regex rather than a copy that could drift from it.
# grep's own no-match under pipefail would abort the script here before the emptiness
# check below ever runs — tolerate that specifically, let the check report it properly.
bots=$(grep -o "bots='[^']*'" .claude/skills/quality-audit/scope.sh | sed "s/^bots='//; s/'\$//" || true)
if [ -z "$bots" ]; then
  echo "FAIL: could not extract the bots pattern from scope.sh"
  exit 1
fi

cases_exclude=(
  "abc1234 chore(deps): bump foo"
  "def5678 fix(deps-dev): bump bar"
  "111aaaa dependabot: something"
  "222cccc renovate: bump docker image"
)
cases_keep=(
  "333bbbb chore: unrelated cleanup"
  "444dddd chore(deps-something-else): looks close but different"
  "555eeee feat: a real feature"
  # dependabot/renovate mentioned mid-message, not as the commit's own type prefix — the
  # regression this anchoring fix exists for: a real commit about Renovate's own config
  # must not be misread as a bump Renovate made.
  "666ffff feat: add renovate docs page"
  "777aaaa chore: bump renovate config"
  # A hex-looking token appearing mid-subject is not this commit's own hash — the
  # hash-prefix branch must anchor to the true line start, not match anywhere.
  "abc1234 feat: mention deadbeef renovate cache fix"
  # An untyped real commit that merely starts with the same letters as a bot name — this
  # fleet's actual bot commits all read "dependabot: ..." / "renovate: ...", so a delimiter
  # is required after the bot-name branch too, not just the start-anchor before it.
  "abc1234 Renovated the audit dashboard"
)

for line in "${cases_exclude[@]}"; do
  if echo "$line" | grep -viE "$bots" >/dev/null; then
    echo "FAIL: expected to EXCLUDE (bot bump), but it survived the filter: $line"
    fail=1
  fi
done

for line in "${cases_keep[@]}"; do
  if ! echo "$line" | grep -viE "$bots" >/dev/null; then
    echo "FAIL: expected to KEEP (real commit), but the filter excluded it: $line"
    fail=1
  fi
done

# FEATURE THEMES classification: a real regression once had every line fall through
# unclassified (the always-succeeding hash-strip poisoned the t-flag ahead of the
# paren-pattern check, because both substitutions shared one sed invocation). Run the
# SHIPPED script end to end against a throwaway git repo with controlled commits, so a
# revert to the single-invocation form fails this test rather than just a duplicated
# pattern under test.
here="$(pwd)"
# Declared and trapped before either mktemp runs: if the trap were set only after both
# succeeded, a failure in the *second* mktemp would exit (set -e) before the trap exists,
# leaking the dir the first one already created. rm -rf on an empty/unset path is a no-op.
fixture_repo=""
fixture_scope=""
trap 'rm -rf "$fixture_repo" "$fixture_scope"' EXIT
# Explicit template: GNU mktemp defaults one when none is given, but BSD/macOS mktemp
# requires either a template or -t and errors ("too few X's in template") without one.
fixture_repo=$(mktemp -d "${TMPDIR:-/tmp}/quality-audit-test.XXXXXX")
fixture_scope=$(mktemp -d "${TMPDIR:-/tmp}/quality-audit-test.XXXXXX")
git -C "$fixture_repo" init -q -b main
# log.decorate is unset by default, but plenty of real ~/.gitconfigs turn it on for nicer
# interactive log output — and `git log --oneline` picks it up too. Set it here so this
# fixture exercises the case where decoration text ("(HEAD -> main)") lands between the
# hash and the subject on the very commit HEAD points to, the exact shape that would slip
# past the bots regex's hash-prefix anchor if `--no-decorate` weren't forced below.
git -C "$fixture_repo" config log.decorate short
git -C "$fixture_repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m 'base'
git -C "$fixture_repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m 'feat(auth): add login'
git -C "$fixture_repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m 'chore: cleanup'
git -C "$fixture_repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m 'random text no colon'
git -C "$fixture_repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m 'chore(deps): bump some-dep'
# scope.sh's own exit status must be checked on its own, same reasoning as scope.sh's two
# internal git-log/grep checks: a `|| true` on the whole pipeline would swallow a genuine
# scope.sh crash (bad range, missing git) as silently-empty $themes, and the FEATURE THEMES
# comparison below would then report a misleading "classification wrong" instead of the
# real fatal error.
if ! scope_out=$(cd "$fixture_repo" && bash "$here/.claude/skills/quality-audit/scope.sh" HEAD~4 HEAD fixture "$fixture_scope"); then
  echo "FAIL: scope.sh itself failed against the fixture repo (see its fatal error above)"
  fail=1
  scope_out=""
fi
# Same reasoning as scope.sh's own bots-filter step: an empty FEATURE THEMES section (the
# exact regression this whole fixture exists to catch) would make grep's no-match abort the
# script under pipefail, before the diagnostic comparison below ever runs — tolerate only
# that, now that a genuine scope.sh failure is caught above instead of hiding behind it.
themes=$(printf '%s' "$scope_out" \
  | sed -n '/FEATURE THEMES/,/REVIEW-FEEDBACK/p' | grep -E '^ *[0-9]+ ' | sed -E 's/^ *[0-9]+ //' || true)
expected=$(printf 'auth\nchore\n(untyped)')
# The bot-bump commit above is HEAD when scope.sh runs, so with decoration on it's the one
# commit whose --oneline line would carry "(HEAD -> main)" if --no-decorate were missing.
# If the exclusion breaks under decoration, this commit leaks into the audited file and
# gets classified as "deps" — failing the FEATURE THEMES comparison below on its own, but
# assert on the file directly too for a clearer failure message.
if grep -q 'bump some-dep' "$fixture_scope/commits-fixture.txt"; then
  echo "FAIL: bot-bump commit survived the filter under log.decorate=short (decoration text broke the hash-prefix anchor)"
  fail=1
fi
if [ "$(echo "$themes" | sort)" != "$(echo "$expected" | sort)" ]; then
  echo "FAIL: FEATURE THEMES classification wrong; got:"
  echo "$themes" | sed 's/^/  /'
  fail=1
fi

# Path traversal in the label arg: it's interpolated straight into "$out/commits-$label.txt",
# so an unvalidated label containing "../" could write outside the intended scratch dir —
# quoting the variable reference doesn't change what the resulting path string resolves to.
# Assert on the specific error, not just "the script failed" — any unrelated failure (a bad
# ref, a missing fixture) would also exit non-zero and let a real regression here hide.
traversal_err=$(bash "$here/.claude/skills/quality-audit/scope.sh" HEAD~2 HEAD "../../../tmp/pwn" "$fixture_scope" 2>&1 >/dev/null || true)
if ! echo "$traversal_err" | grep -q "label must be letters, digits"; then
  echo "FAIL: scope.sh did not reject the path-traversal label with the expected validation error; got:"
  echo "$traversal_err" | sed 's/^/  /'
  fail=1
fi

# `head -N` after `sort -rn` can SIGPIPE-abort under pipefail: head closes its read end
# once satisfied, and on a large enough footprint sort can still be mid-write when that
# happens (empirically confirmed unreliable below ~20k lines — not worth a slow, flaky
# fixture here; a static check on the actual fix is the honest, fast alternative).
if grep -qE 'sort[[:space:]]+-rn[[:space:]]*\|[[:space:]]*head' .claude/skills/quality-audit/scope.sh; then
  echo "FAIL: scope.sh uses 'sort -rn | head', the form that can SIGPIPE-abort under pipefail — use awk 'NR<=N' instead"
  fail=1
fi

if [ "$fail" -eq 0 ]; then
  echo "OK: scope.sh copies match; bot-bump filter correct on ${#cases_exclude[@]} exclude + ${#cases_keep[@]} keep cases; feature-theme classification correct; label validated; no SIGPIPE-prone head"
fi
exit "$fail"
