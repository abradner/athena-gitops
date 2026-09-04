---
name: resume-context
description: Pick up work from a handoff file written by park-context — read it, verify its claims against the current repo state before trusting any of them, report drift, and restart on the recorded next action. Use after compacting or clearing, in a fresh session on a branch that has a handoff, or when the operator says "pick up where we left off", "resume from the handoff", "read the handoff", or pastes a path under .claude/handoffs/.
---

# Resume context

A handoff is a **claim, not a verdict** — `AGENTS.md`'s review-feedback rule applies to it exactly
as it does to a bot finding, and more sharply: the session that wrote it is gone and cannot be
asked what it meant. It was accurate when written, under budget pressure, possibly about a tree
that has since moved. Verify before building on it.

The failure this prevents: a resumed session reads "tests pass, next step is X", starts X, and
discovers an hour later that the tree changed, the branch moved, or the claim was in the
**Assumed** section all along.

## Procedure

1. **Find the handoff.** Use the path if the operator gave one. Otherwise the newest file in
   `.claude/handoffs/` — list it and name which one you picked; do not silently choose among
   several. If more than one is plausibly current, ask. Remember the worktree split: handoffs are
   written to the `.claude/handoffs/` of whichever worktree the session ran in, and a git worktree
   has its own working tree wherever it happens to live — so a handoff written in one is invisible
   from the primary checkout. Look in the worktree the branch actually lives in (`git worktree
   list`), not just the one you are standing in.

   If there is no handoff at all and the operator is referring to prior work, **do not conclude the
   context is gone until you have looked.** A session that forked rather than continued still has
   its raw transcript on disk (`~/.claude/projects/<project>/*.jsonl` — grep or timestamp-match for
   the predecessor), and anything a commit or PR references by number has its real scope in the
   tracker (`gh issue view <n>`, not `gh issue list` — titles and "Closes #N" lines compress away
   exactly the deferred scope that mattered). Check both before saying you can't recover it.

2. **Read it whole, plus `AGENTS.md`.** Both, before touching anything. `AGENTS.md` is the
   authority on how to work here; the handoff is the authority only on what this task is.

3. **Verify before trusting.** Re-run the state probe and reconcile it against the handoff's
   **State** section:
   ```
   git status --short --branch && git log --oneline -8 && git diff --stat HEAD
   ```
   - Everything under **Assumed** stays assumed. Do not promote it because it reads confidently.
   - Everything under **Verified** is re-checked if the tree moved since it was recorded, and
     re-checked outright if acting on it is expensive or irreversible.
   - **In flight when interrupted** is the priority: establish what a half-applied edit or an
     unread background job actually left behind before doing anything else. Resolving it may be
     the real next action, ahead of the recorded one.

4. **Report drift explicitly.** Before proposing work, state in a few lines: what the handoff
   claimed, what is true now, and what that changes. Silence here is how a stale premise becomes
   this session's foundation. If the drift is large enough that the recorded next action no longer
   makes sense, say so and stop rather than improvising a substitute.

5. **Discharge the Ratchet.** The handoff's **Ratchet** section names durable items — `AGENTS.md`'s
   Gotchas & Lessons Learned entries, a new convention this work proposed, tracker items — that
   were parked rather than filed. Land them, or carry them forward explicitly into the next
   handoff. A ratchet item
   silently dropped at resume is the failure mode the section exists to prevent: it means the
   lesson was learned, written down twice, and lost anyway.

6. **Surface open questions, then confirm.** Put the handoff's **Open questions** to the operator.
   Restate the next action and get a go-ahead before starting it — the operator's picture of where
   things stand may differ from the handoff's, and this is the cheapest moment to find that out.

7. **Retire the handoff** once the work it describes has landed — but **ask before deleting it.**
   You did not create this file; a previous session did, and `AGENTS.md` §8 requires explicit
   approval for deleting files you didn't just create, every time. The work landing is not that
   approval. Say the work has landed, name the file, and propose removing it.

   It does need to go: handoffs are gitignored local state, so nothing else will ever clean them
   up, and a stale one is a trap for the next session — it has no way to know it is describing
   finished work. If the operator isn't around to ask, mark it stale in place (a
   `> RETIRED — landed as <ref>` line at the top) rather than deleting unilaterally. Parking again
   mid-task writes a new file; it does not update the old one.

## Rules of thumb

Name the file you picked. Read `AGENTS.md` too. Verified gets re-checked, assumed stays assumed,
in-flight gets resolved first. Report drift before proposing work. Discharge the ratchet or carry
it forward. Confirm the next action with the operator. The handoff has to go once the work lands —
nothing else will clean it up — but you did not write it, so propose the deletion rather than
performing it.
