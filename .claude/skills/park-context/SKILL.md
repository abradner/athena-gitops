---
name: park-context
description: Park a session's un-recoverable context into a durable handoff file so the operator can compact immediately without losing the thread. Use when the operator says they are about to run out of context, are at 80/90/95%, want to compact or clear, says "park the context", "persist everything important", "save state before compacting", "write a handoff", or interrupts a long turn to preserve state. This is for a PLANNED compaction of a live session — for reconstructing a session that was already lost or crashed, that is a different job.
---

# Park context

The operator is about to compact. Compaction keeps a summary and discards the transcript, so
**anything not written down in the next few minutes is gone permanently.** This skill exists to
make that moment mechanical instead of a scramble, and to make the resulting file worth trusting.

The bargain: **write only what compaction destroys.** Everything reconstructible from the repo —
file contents, diffs, commit messages, PR bodies, what a function does — is not at risk and does
not belong in the handoff. What is at risk is the knowledge that lives only in the conversation:
intent, rejected alternatives, what was actually verified, and what was mid-flight when the turn
was cut — the knowledge that lives *between* the files and can't be reconstructed by grep, applied
to a session instead of a codebase.

## Budget: this runs at 80–95% context

Every token spent here is one the handoff can't use. Hard constraints, no exceptions:

- **Do not explore.** No re-reading files, no greps, no `gh pr view`, no subagents, no "let me
  just check". Everything in the handoff comes from what is already in this context window.
- **One state probe only** — a single batched shell call, output not analysed beyond transcription:
  ```
  git status --short --branch && git log --oneline -5 && git diff --stat HEAD
  ```
  It exists to stop the handoff claiming a tree state that isn't true. Add `gh pr list --json
  number,title,headRefName` only if this session has open PRs in play.
- **Do not finish the work.** A half-applied fix started at 93% is worse than none. Park the
  half-applied state as an observed fact; do not tidy it, revert it, or complete it.
- **Do not commit, push, or merge.** Parking is not a checkpoint for published state.

## Procedure

1. **Probe state** — the one command above.

2. **Write the handoff** to `.claude/handoffs/<YYYYMMDD-HHMMSS>-<branch-slug>.md` (UTC; in a
   worktree, that means the worktree's own `.claude/`, which is correct — the work is
   branch-scoped).

   **One file per park; never overwrite a previous one.** Seconds are in the stamp because a
   minute-resolution name collides when the same branch is parked twice in one minute (an
   interrupted park retried, or two sessions on one branch), and the thing it would silently
   overwrite is the *only* durable copy of the earlier session's irrecoverable context.

   Seconds make that collision unlikely; they do not make it impossible, so reserve the name
   atomically — noclobber creation fails on an existing path instead of truncating it, which an
   existence check alone cannot guarantee (two same-second parks can both pass the check):

   ```bash
   mkdir -p .claude/handoffs   # gitignored, so absent in a fresh clone or new worktree
   base=".claude/handoffs/$(date -u +%Y%m%d-%H%M%S)-<branch-slug>"
   f="$base.md"; n=2
   until (set -C; : > "$f") 2>/dev/null; do f="$base-$n.md"; n=$((n+1)); done
   ```

3. **Ratchet what's durable.** A lesson learned this session dies with the handoff file unless it
   reaches its permanent home. If it is a one-line append and the tree is otherwise clean, write
   it straight into `AGENTS.md`'s Gotchas & Lessons Learned section now. Otherwise record it in
   the handoff's **Ratchet** section, which the resume skill is required to discharge. Discovered
   work goes to the external tracker named in `AGENTS.md`, not into the handoff — a handoff is not
   a tracker, and it is deleted when the work lands.

4. **Report and stop.** Print the path, a 3–6 line summary of what was parked, and the exact line
   the operator can paste after compacting:
   ```
   read .claude/handoffs/<file>.md and pick up where we left off
   ```
   Then stop. Do not start the next step of the work — the operator compacts next.

## The handoff file

Sections are load-bearing; keep the headings even when a section is empty (an empty **Verified**
section is itself a finding). Prose over bullets where reasoning matters; bullets for state.

```markdown
# Handoff — <task in one line>
Parked: <UTC timestamp> | Branch: <branch> | Repo/worktree: <path>

## Goal
What we are actually trying to achieve and why, in the operator's framing. Include the original
ask close to verbatim if it constrains the work.

## Decisions & rejected alternatives
The highest-value section — this is what compaction destroys most reliably. Each: what was
decided, and what was considered and rejected, with the reason. A decision without its rejected
alternatives gets re-litigated in the next session.

## State
- **Verified:** claims this session actually observed, each with what was run and when.
  "Tests pass" is not a state; "`<cmd>` exited 0 at 14:20, before the edits to <file>" is.
- **Assumed:** believed but never checked. Mark them; a resumed session must not promote these.
- **In flight when interrupted:** half-applied edits, background jobs, subagents whose output was
  never read. Record as observed, not as intended.

## Next action
The single next concrete step, specific enough that a cold session can begin without deciding
anything. Then what follows it, briefly.

## Landmines
What burned us this session: approaches that failed and why, commands that misled, tools that
reported success without doing the thing. Saves the next session from re-running them.

## Ratchet
Durable items that must land before this work merges: `AGENTS.md`'s Gotchas & Lessons Learned
entries, a new convention this work proposed, tracker items not yet filed. Empty is fine;
unrecorded is not.

## Open questions for the operator
Things blocked on a human decision. Never resolve these by guessing at park time.

## Verbatim — do not paraphrase
Exact strings that lose their value when reworded: error text, IDs, PR/issue numbers, SHAs, file
paths, commands, config snippets. Compaction paraphrases; this block is the quarantine that
survives it.
```

## What not to write

- Anything reconstructible: diffs, file contents, commit messages, PR bodies, code explanations.
  A handoff that recaps the diff is long, stale on arrival, and crowds out the irrecoverable part.
- A chronological narrative of the session. The next session needs the current position and the
  reasoning behind it, not the path taken to reach it.
- Confidence the session doesn't have. If something was never verified, it goes under **Assumed**
  — a handoff is read as fact by a session with no way to check it, so an unmarked guess becomes
  a false premise with no author left to challenge.

## Rules of thumb

Write only what compaction destroys. One state probe, no exploration. Don't finish the work, don't
commit, don't push. Verified and assumed are different sections. Rejected alternatives are the
point. Durable lessons go to their real home or into Ratchet. Verbatim strings go in the
quarantine block. Print the path and the paste-line, then stop.

## Where these rules came from

Append this repo's own parking outcomes here as they accumulate — add rather than replace.

- The no-exploration rule exists because the natural instinct at 90% context is to go re-read the
  files to write an accurate summary, which spends the remaining budget producing a handoff that
  then gets truncated mid-write. The information needed is already in the window.
- The verified/assumed split exists because the standard failure of a handoff is a state claim
  that was true three edits ago. `AGENTS.md`'s "verify the output, not the instrument" applies
  with extra force here: the resumed session has no transcript to check the claim against.
- The verbatim block exists because compaction is a paraphraser. An error string, a PR number, or
  a SHA that gets summarised into "the failing test" or "the relevant PR" cannot be recovered.
- The don't-finish-the-work rule exists because parking is usually triggered by interrupting a
  long turn — the tree may hold a partial edit nobody intended. Completing it under budget
  pressure produces exactly the unreviewed change the next session will trust and not re-check.
