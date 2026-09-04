---
name: quality-audit-refresh
description: Fold newly-landed tranches into an existing quality audit — dig the new ranges with the fleet, RE-VERIFY every open item in the audit doc against the new HEAD (so the followup ledger stays true), score the trajectory against the audit's named patterns, and update the doc + Artifact + PR in place. Use when the user says main has moved on since the audit, asks to "iterate on the audit", "audit the new tranches", "refresh the guide/ledger", or wants a clear artefact of remaining followup work once a flurry of development lands. For a first audit from scratch use quality-audit.
---

# Quality audit — refresh

Inputs: the existing `docs/audit/<doc>.md`, its Artifact URL (memory:
`<repo>-quality-audit-<yyyy-mm>` or `Artifact action:list`), and the range
from the doc's recorded HEAD to the new target.

## 1. Scope and split the new range

```bash
.claude/skills/quality-audit-refresh/scope.sh <docHead> <target> all <scratch>
```
Then split `commits-all.txt` into thematic tranches by PR/commit-scope
grep (feature branches and PR stacks interleave on main, so tranches here
are thematic within one git range — say so in the doc). Number them
continuing from the doc (chronological). Write one `commits-<Tn>.txt` each.

Same establish-before-first-use notes as `quality-audit`'s cold start apply here — the migration
and contract-path detection live in this copy of `scope.sh`, adapted the same way if this repo's
copy of `quality-audit` was.

## 2. Extract the open ledger

```bash
.claude/skills/quality-audit-refresh/extract-open-items.py docs/audit/<doc>.md <scratch>
```
→ `open-items.json` (P1s, P2s, per-tranche findings, new-debt paragraphs,
every scoreboard missing-piece). Its count is `openCount`.

## 3. Rebase, then run

`git rebase <target>` on the audit branch first — agents read files at the
checkout, and a stale checkout silently audits old code.

Args: `dir, head, range, tranches[{key, era, commitsFile, slices}],
openItemsFile, openCount, priorPatterns[], numberingNote, deltaFocus, diggerModel, tracker`.
`priorPatterns` = the doc's structural themes each with the verdict the
*last* refresh gave it. `deltaFocus` = the one question the new work most
directly answers (e.g. "did the primitives extraction resolve the twins").
`diggerModel`/`tracker` are the same knobs as `quality-audit`'s cold start — carry the same values
forward on a refresh rather than re-asking.

```
Workflow({ scriptPath: ".claude/skills/quality-audit-refresh/refresh.workflow.js", args })
```

Slicing rules are the same as the cold-start skill; every new-tranche
slice prompt should name the *known* gaps it might close or repeat, so the
digger reports CLOSES / REPEATS / NEW explicitly.

## 4. Synthesise

1. **Ledger first.** From `reverified`: mark each open item fixed (with
   `fixed_by`), partially fixed, invalid, or still open. Anything the
   re-verify pass flips is more valuable than any new finding — it is what
   makes the doc a followup artefact rather than a snapshot. A result of
   `unverified` means the *reverify* agent (Phase 2, not the aggregate Verify
   pass in Phase 4) either omitted that id from its response, or its whole
   call for that chunk came back empty (the structured-output retry cap) —
   either way it is not a verdict of "still open"; carry the item's prior
   ledger status forward unchanged rather than overwriting it (the extractor
   re-pulls items regardless of prior status, so an unverified id can
   already be `fixed` or `invalid` there).
2. New tranches: same shape as cold start (findings with verdicts, completeness
   rows, slice health). Add their findings and gaps to the ledger as open.
3. Trajectory: append a row per pattern to the trajectory table (direction +
   evidence for the new tranches), rewrite the verdict paragraph, keep the
   prior tranches' verdicts visible so the trend reads.
4. Hand spot-check the headline delta claim and one "fixed" flip.
5. Update the doc header (ranges table, HEAD, agent counts), the scoreboard,
   next steps; republish the Artifact to the SAME URL (read it back with
   `action:"read"` if the scratch source is gone); edit the PR body; update the memory pointer.

**If the audit branch already backs an open PR, the preceding rebase rewrites published history —
report readiness and get an explicit, current go-ahead before the force-with-lease push, the same
gate every other rewrite of published history in this template needs.** A synthesis pass finishing
is not that signal.

## Gotchas already paid for

- A feature branch merged as one PR shows up to the completeness assessor as
  `PRs []` per feature. Put the merge PR number in the tranche `era` text and
  tell the assessor the individual commits carry the promises.
- Before crediting a fix to a "follow-up stack", run `gh pr list --state all`
  — a refresh once found a whole stack the assessor described as landed that
  was actually still open (never cite the stack by a bare PR-number range;
  numbers are repo-local and this doc outlives any one repo's numbering).
  The ledger has an **in flight** status for exactly this; use it rather
  than `fixed`.
- Verifiers sometimes confirm the facts of a finding but dispute its severity
  framing (e.g. "security" → "product integrity"). Keep the finding, carry the
  verifier's reframing into the doc.

- Resume replays only the unchanged prefix of agent() calls — a mid-run
  failure means everything after it re-runs. Put the expensive digs first.
- A session-limit hit kills all agents with 0 tokens; relaunch, nothing lost.
- Renumbering: `\bT[0-9]\b` with a placeholder pass (simultaneous mapping),
  then fix heading anchors (`#trajectory-does-tN-…`).
- Copilot review comments on the PR: verify each against code; when one is
  right it often exposes a *stale* finding rather than a wrong one.
