---
name: quality-audit
description: Cold-start a fleet code-quality audit over a set of git ranges ("tranches") — subsystem diggers on a cheaper model, adversarial verifiers, a feature-completeness assessor per tranche, cross-cutting coherence reviewers — then synthesise into docs/audit/<yyyy-mm>-quality-audit.md plus a published Artifact. Use when the user asks to audit recent work for quality, correctness, architecture, smells, maintainability, simplicity, security or "how finished is this really", names commit ranges or eras of work to review, or wants "a fleet of agents to dig and a stronger model to synthesise". For adding NEW tranches to an EXISTING audit, use quality-audit-refresh instead.
---

# Quality audit — cold start

Output: `docs/audit/<yyyy-mm>-quality-audit.md` on a branch + PR, and a
private Artifact page. Both carry the same content; the doc is condensed.
The user must have opted into multi-agent orchestration (they will have —
this skill only exists because they asked for a fleet).

## 0. Decisions to take from the user before anything runs

Ask once, together (AskUserQuestion): digger model tier (default Sonnet),
report home (default both), whether security is a full dimension (default
yes — one dedicated security slice per tranche). Everything else is yours.

## 1. Scope the ranges

```bash
.claude/skills/quality-audit/scope.sh <base> <target> <label> <scratch-dir>
```
once per tranche. It writes `commits-<label>.txt` with Dependabot/Renovate
bumps stripped and prints what decides the slicing: footprint by directory,
migrations, new rake/bin scripts, payload/contract version bumps, review-feedback
batches (a lot of first-pass findings are already fixed in those).

**Establish before first use in a repo.** The migrations check tries `db/migrate` and
`migrations`; add another convention to the script's `migrate_dirs` line if this repo uses one.
Payload/contract version-bump detection is opt-in: list the files that carry a version constant,
one per line, in `.claude/skills/quality-audit/contract-paths.txt` — absent that file, the check
is skipped rather than silently checking nothing and looking thorough.

**Number tranches chronologically, T1 = oldest.** New work appends as Tn+1.
(The first audit numbered newest-first and had to flip; don't repeat that.)

**Check the working tree is at the target HEAD before launching.** Diggers
read files "at HEAD" through the checkout. If the audit branch sits behind
main, `git rebase <target>` first — otherwise every file read is stale and
only the `git diff <range>` calls are right.

## 2. Slice the fleet

Rules of thumb that held:
- One digger per subsystem×tranche, ≲150 files / ≲10k lines each. A 700-file
  tranche wants 7–8 slices; a 75-file one wants 4–5.
- One **security** slice per tranche with the whole-tranche diff and a threat
  list written for that tranche (new principals, new cookies, new channels,
  new JSON writes, new scripts that touch prod data).
- One **completeness assessor** per tranche (Sonnet): reads PR bodies via
  `gh pr view`, tries the configured tracker (`args.tracker`, or this repo's
  own `AGENTS.md` if unset), consumes the diggers' `loose_ends`, scores
  each feature % complete with ticket-ready missing pieces, and says whether
  each deferral has a real ticket or just a prose mention.
- Three **coherence** lenses on the session model over all tranches:
  frontend conventions, backend/service architecture, test strategy.
- Verify chunks of 5 high/medium findings, session model, prompted to REFUTE.

## 3. Run

Build the args object (tranches, slices, lenses, `dir`, `head`, `diggerModel` — the tier chosen in
step 0, or omit for the default; `tracker` — the tracker named in this repo's `AGENTS.md`, or omit
to have each completeness assessor read `AGENTS.md` for itself) and launch:

```
Workflow({ scriptPath: ".claude/skills/quality-audit/cold-start.workflow.js", args })
```

Expect 30–45 agents, ~3–6M subagent tokens, 20–35 min. Known failure modes:
- **Structured-output retry cap** on one digger → its slice returns `null`.
  Resume re-runs it, but only the unchanged *prefix* of agent() calls replays
  from cache; everything after re-runs (and can produce different, often
  thinner, results). Treat run 1 as primary and splice the repaired slice in.
- **Session limit** → every agent dies at launch with 0 tokens. Just relaunch
  with `resumeFromRunId` after the reset.
- If the configured tracker is behind an interactively-authenticated MCP
  connection (Notion and similar are common examples), it may be unreachable
  from agents; the assessor is told to say so and fall back to PR bodies.

## 4. Synthesise (main thread — this is the expensive-model work)

1. Distil the result JSON into per-tranche digests (confirmed/fixed_later
   findings with verifier notes, lows, refuted, completeness, slice health).
2. **Hand spot-check 2–3 headline findings** by reading the code. Verifiers
   are good, not infallible; the first audit's Copilot review found six wrong
   claims — five were digest compression errors, one was staleness.
3. Rank: P1 (correctness/security that bites), P2 (confirmed, less urgent),
   structural themes (from coherence), code-health section (duplication
   census, dead-code census, typing debt map, smell signature, per-layer
   health map — the first cut compressed this away and the user asked for it
   back), completeness scoreboard, "what the process got right", next steps.
4. Every `file:line` cited must exist at HEAD — grep them.
5. Doc → `docs/audit/…`; Artifact → load `artifact-design`, utilitarian
   treatment, both themes, republish to the same URL on every edit.
6. Commit on a branch, `gh pr create`, save the artifact URL in memory.

Keep the artifact source under the scratch dir AND expect it to vanish on a
worktree recycle: `Artifact action:"read"` returns the raw HTML to rebuild from.

## 5. When review comments arrive on the PR

Verify each against code before accepting — Copilot went 6/6 on the first
audit, but only because each claim was checked. Fix, reply on the thread,
resolve, and keep doc/artifact/PR-body counts in agreement.
