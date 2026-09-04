---
name: single-pr
description: Ship one self-contained change as a single PR with the batch flow's rigour - self-review before opening so findings cost an amend not a round, solicit the reviewers that don't fire on their own, harvest all three comment surfaces, react immediately under a three-round cap, verify before claiming green, and merge only on a live operator go-ahead. Use for any ordinary change that fits in one PR - a feature, a fix, a refactor, a docs pass - which is this repo's default flow. Use `.claude/skills/batch-review` instead when the work is genuinely several PRs opened together as one body of work.
---

# Single PR

The default flow: **react to feedback immediately, merge when green.** One PR at a time, so there
is no reason to withhold reaction — that discipline exists only to stop churn across several
in-flight PRs, and it is the wrong shape here.

This skill exists because the default being *simple* is not the same as it being *rigorous*. Almost
everything that makes `batch-review` work is not about batching at all: it is about getting the
change actually reviewed and actually verified. That machinery is shared, and lives in
**`docs/pr-review-machinery.md`** — read it now, before Phase 1. This skill covers only what is
specific to shipping one PR.

## When to use the other one

Escalate to `.claude/skills/batch-review` when the work is genuinely several PRs opened together —
a planned fan-out, an overnight batch, a stack. The tell is *reviewer attention fragmenting across
live threads*, not the size of the diff: one large coherent PR belongs here.

When this PR carries several commits and the work was substantial, delegated, or touches a trust
boundary, put it through an independent fresh-eyes pass **before opening** — one reviewer per
commit, with no memory of writing them (`.claude/skills/independent-commit-review` if this repo
kept it). A PR is one review surface; commits inside it get read as a unit, so a defect in commit 2
rides in on commit 5's plausibility.

## Phase 1 — Self-review before opening

Run one pointed question over the diff: **"what did this change make newly risky?"** Fix what it
finds in the commit itself.

This is the highest-leverage phase and the easiest to skip. A finding surfaced before the PR exists
costs a `git commit --amend`; the same finding after opening costs a reactive round, and rounds
introduce bugs (see the round cap in the shared doc). Delegate it to a cheap review subagent if one
is available — the point is a second pass, not an expensive one.

For anything touching an access-control or trust boundary, the mandatory question is: **who is the
caller, how do we know, and what happens if they lie?** "We trust what they tell us" is the finding,
not a stub to note and move past.

## Phase 2 — Open it

- **Open ready-for-review, not draft** — which reviewers fire on which edge is in
  `docs/pr-review-machinery.md` §1, and is not restated here.
- Run the repo's full pre-push gate first (lint, build, tests). Interstitial CI red is tolerable in
  a batch because a followup fixes it; here there is no followup.
- **Sweep neighbouring open PRs** for file overlap (`gh pr list`, then `gh pr view <n> --json
  files`). Default disposition is **comment and let them adapt after this lands** — never absorb
  their work, never pre-emptively rebase their branch. Irreversible collisions (competing migration
  timestamps) are operator decisions.
- The PR body states what changed and why, and names anything the diff cannot show: a convention
  this PR is proposing (see `AGENTS.md` §8 — unstated patterns are proposals), an assumption, a
  deliberate omission. The test-plan section states what was actually run, honestly — including
  what could **not** be verified in this environment.

## Phase 3 — Solicit review

Work from **this repo's** reviewer table in `docs/pr-review-machinery.md` — it is rewritten at
template init and may list different bots, or none. Do not assume a roster from memory. Ask for
whichever entries don't fire on their own, then wait ~10 minutes before looking.

Then **harvest all three surfaces** — issue comments, review bodies, and inline comments. The
findings are usually inline. Never conclude "no findings" from an empty review body.

## Phase 4 — React

This is the inversion from `batch-review`, and the only phase that genuinely differs: **respond to
findings as they arrive.** Triage by verifying (a finding is a claim), fix, push.

- **Re-request review after any substantive push.** Copilot does not re-review a pushed branch on
  its own, so without this the approving verdict you eventually read is about superseded code.
- **Three reactive rounds, then defer** to the tracker. If this one change draws three or more
  findings, revert it and ticket it — it needs design time, not another patch.
- Fix bugs with **revert-and-confirm**: write the regression test, confirm it passes with the fix,
  revert just the fix, confirm the test fails *for the right reason*, restore the fix. A test that
  was never seen to fail hasn't proven anything.
- Keep the steering docs current **in this PR** if the change moved a boundary or a documented
  behaviour — `AGENTS.md`, and the repo's boundary map if it keeps one. A doc describing a boundary
  that no longer exists is worse than none, because it is trusted.
- Discovered work that isn't in scope goes to the external tracker, not a PR-body table and not a
  code comment.

## Phase 5 — Verify, then report ready

Work the shared doc's green-signal table before claiming anything is green. Specifically, for one
PR: check **which CI jobs actually ran** rather than the rollup colour, confirm a review was
delivered **for the current head commit** (from the review record — the re-anchor trap is the
shared doc's §2), and name the artifact the change should have produced and go look at it — the
rendered page, the written file, the actual rows.

Then report readiness and **stop**. Readiness is not authorization.

## Phase 6 — Merge

The gate is `AGENTS.md` §8: a live operator go-ahead, with every new session resetting to the manual
gate. A green build, a clean review, and an auto-mode session default are none of them that signal.

On the go-ahead, **first re-run the merge check in `docs/pr-review-machinery.md` §6**: the head you
are merging may not be the head that was reviewed, because anything pushed since Phase 5 — a late
fix, or one made after the go-ahead arrived — moved the branch past what that check saw. Then merge per the repo's stated strategy, delete the branch **only** if
nothing is based on it, and confirm the merge actually landed rather than trusting the command's
return. Finally, restart the working branch from fresh main — never stack new commits on pre-merge
history:

```bash
git fetch origin main && git checkout -B <branch> origin/main
```

If the repo builds artifacts from tags, **a merge is not a release, and the go-ahead to merge does
not authorize one.** Report the merged state and stop. Cutting a tag publishes an artifact, and
`AGENTS.md` §8 requires explicit approval for each outward-facing action — approval for one is not
approval for the next.

## Rules of thumb

Read `docs/pr-review-machinery.md` first. Self-review before opening — an amend beats a round. Open
ready, never draft. Sweep the neighbours; comment, don't absorb. Ask the reviewers that don't fire
on their own, then wait 10 minutes. Harvest all three surfaces; findings are inline. React
immediately, re-request review after every substantive push, three rounds then defer. Revert-and-
confirm every fix. Docs move in the same PR. Check which jobs ran, not the colour. Report ready and
stop — the operator says merge.

## Where these rules came from

Shared evidence lives in `docs/pr-review-machinery.md`. Specific to this flow:

- The self-review gate is the same measured finding as `batch-review`'s: findings are an order of
  magnitude cheaper before the PR exists than after.
- The open-ready-not-draft rule exists because a change can otherwise sit "in review" for an hour
  with nothing having reviewed it. It drew a finding in three consecutive review rounds of the PR
  that introduced it — each time because this skill kept its own copy of the trigger detail. The
  copy is gone; the shared doc owns it.
- The re-request-after-push rule exists because Copilot reviews the ready edge and never a push. A
  batch once read an approving verdict that predated three rounds of changes.
- The commit-granularity escalation exists because an independent per-commit pass over 12 fast-built
  commits found real problems in **9 of them**, including a subject-spoofing auth bypass — none of
  which a single squashed review surface had surfaced.

Append this repo's own single-PR outcomes here as they accumulate.
