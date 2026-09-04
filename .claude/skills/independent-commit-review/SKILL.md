---
name: independent-commit-review
description: Adversarial, fresh-eyes review of already-committed local work before it's pushed or fanned out into PRs - one independent subagent per commit with zero prior context, skeptical "stranger's PR" framing, findings fixed with revert-and-confirm verification, then history rebuilt cleanly via cherry-pick so each commit looks correct on first read. Use before pushing a batch of commits, before a batch-review fan-out, or whenever the user asks for an independent/adversarial/fresh-eyes review of local commits.
---

# Independent commit review

Don't review your own work and call it independent. The session that wrote the code carries its
author's assumptions; this process buys genuinely fresh eyes by giving each commit to a subagent
that has never seen the conversation.

Why it earns its cost: the first repo to run this process reviewed 12 fast-built commits after the
fact and found that **9 of them needed real fixes** — including a subject-spoofing auth bypass
(nobody had asked "how do we know who's calling?" at design time) and an escaping bug that leaked
data through a security filter. The same rigor is far cheaper applied per-commit during the build
than as a reset at the end — but when the reset is needed, this is how to do it safely.

## Procedure

1. **Safety snapshot.** Before any history rewrite: `git branch snapshot/<branch>-<date>` at the
   current tip. The snapshot is kept permanently.

2. **Identify review units.** Default: one unit per commit. Group commits only when tightly
   coupled (a change and its immediate fixup). Skip pure docs/config commits unless they carry
   risk (CI changes, permission changes, anything security-adjacent).

3. **Dispatch one independent subagent per unit, in parallel.** Each prompt gets:
   - the exact commit SHA and instructions to review `git show <sha>` as a stranger's PR;
   - a pointer to read `AGENTS.md` first — the repo's invariants are review criteria;
   - a skeptical, adversarial framing: assume the diff contains at least one real problem and
     hunt for it; concrete things to check for this specific diff, not generic advice;
   - for anything touching an access-control or trust boundary, the mandatory question: *who is
     the caller, how do we know, and what happens if they lie?* "We trust what they tell us" is
     the finding, not a stub to note and move past;
   - a capped output format: severity-ranked findings, ~400–600 words, no praise.

4. **Triage live** as agents report: must-fix in the commit / worth a comment, not code / deferral
   to the tracker. Apply "a finding is a claim, not a verdict" — reproduce before accepting, and
   expect to decline some.

5. **Fix with revert-and-confirm.** For every accepted bug: write the regression test, confirm it
   passes with the fix, revert just the fix, confirm the test fails for the right reason, restore
   the fix. A test that was never seen to fail hasn't proven anything — this step has caught
   broken fixes and broken tests alike.

6. **Rebuild history** so each commit is correct on first read — no trailing "fix review
   comments" commits. On a fresh branch from the original base: cherry-pick each commit in order,
   amending the fixes into the commit they belong to. Resolve lockfile/manifest conflicts across
   amended commits toward the final intended state.

7. **Verify clean, then swap.** Full test suite from a clean state (fresh DB/services if the
   suite uses them). Then confirm the worktree itself is clean — `git status --short` must be
   empty, and abort if it is not: the snapshot branch preserves commits, not uncommitted edits,
   and the swap's `git reset --hard` destroys those silently. Only then point the real branch at
   the rebuilt history, keeping the snapshot branch from step 1 forever.
