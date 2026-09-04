# PR review machinery

> Shared reference for `.claude/skills/single-pr` and `.claude/skills/batch-review`. Both skills
> instruct you to read this. They may *name* a mechanic in passing so their own procedure reads
> straight through, but the detail lives here and only here — **where a skill and this file
> disagree, this file wins, and the skill is the bug.** Never grow a second copy of a rule in a
> skill.
>
> It covers the mechanics of getting a PR actually reviewed and actually verified — the parts that
> are identical whether you are shipping one PR or a stack of them. What differs between the two flows (when you react to feedback, and how the
> merge happens) stays in each skill.

Read this once per session, not once per PR.

## 1. Reviewers: know each one's trigger

Reviewers are not interchangeable and most do **not** fire on their own. Establish for each one
*what triggers it* and *what it is good for* before you need it.

> **Verified for this repo at retrofit time (2026-09-04)** against its own PR history — not
> copied from the shared template unchecked. Re-verify whenever this feels stale, especially
> after adding or removing a reviewer app: confirm each bot has actually commented in *this* repo
> before counting it as a review surface, and check **all three surfaces** from §2 — a bot that
> only leaves inline or issue comments never appears in `pulls/<n>/reviews`, so a reviews-only
> check can report a reviewer as absent when it is working, or as present when it has only ever
> errored:
>
> ```bash
> for n in <recent PR numbers>; do
>   gh api repos/{owner}/{repo}/pulls/$n/reviews  --jq '.[].user.login'
>   gh api repos/{owner}/{repo}/pulls/$n/comments --jq '.[].user.login'
>   gh api repos/{owner}/{repo}/issues/$n/comments --jq '.[].user.login'
> done | sort -u
> ```

| Reviewer | Trigger | Cost | Notes |
|---|---|---|---|
| **Copilot** | Balanced review automatically when a PR is **opened ready for review, or flipped draft→ready**. Never on a draft, and never on push. | Cheap — unrationed | **Confirmed active** — reviewed every recent human PR checked (#94, #91, #88, #87). A followup push needs an explicit re-request through the GitHub PR review mechanism, or you are reading a verdict on superseded code. |
| **Codex** | **Unconfirmed in this repo** — no evidence of any review, ever, across the PR history checked. Not a claim it wouldn't work if enabled; it has simply never been invoked here. If you turn it on, check its own "About Codex in GitHub" box on its first review, which states the repo's actual triggers, and update this row. | Expensive — budget it | Until confirmed, treat its absence as expected, not a process failure — don't wait on a Codex pass that has no basis to arrive. |

Re-requesting Copilot through the API needs the literal `[bot]` suffix on the login:

```bash
gh api repos/{owner}/{repo}/pulls/<n>/requested_reviewers -X POST \
  -f 'reviewers[]=copilot-pull-request-reviewer[bot]'
```

Without the suffix it returns **422 "Reviews may only be requested from collaborators"** — which
reads like a permissions problem but is really a bad-login problem. Don't conclude from that 422
that the route is closed. `GET`-ing the PR back may show `requested_reviewers: []` even on success;
verify via the GraphQL timeline (`ReviewRequestedEvent`) instead.

Rules that hold for every reviewer:

- **Ask for comments only, never commits.** An agent pushing to your branch rewrites work under a
  review in progress.
- **A draft PR gets no *automatic* review** until the ready flip — trigger-on-ready reviewers do
  not fire, so a change can sit looking normal for an hour with nothing happening. An **explicitly
  requested** reviewer will review a draft, which is what makes `batch-review`'s draft-first
  aggregate pass work. Either way, verify a review actually arrived rather than assuming.
- Feedback typically lands ~5 minutes after the trigger; don't look before ~10. If a bot hasn't
  posted within 10 minutes of the trigger (or of the 👀 reaction appearing), assume it won't.
- **Verify the roster is actually installed** — the blockquote above is the check, not a formality.
- **In-session self-review is not an independent pass.** An author reviewing their own work is
  anchored — they already decided the tricky bits were fine while writing them. An independent
  pass by a reviewer with no memory of writing the code is a different instrument
  (`.claude/skills/independent-commit-review`, where a repo keeps it).

## 2. Harvest all three surfaces

They are separate API routes, and the substance is rarely in the one reached for first:

```bash
gh pr view <n> --json comments                     # issue comments
gh api repos/{owner}/{repo}/pulls/<n>/reviews      # review bodies
gh api repos/{owner}/{repo}/pulls/<n>/comments     # INLINE — usually where the findings are
```

Codex posts its findings as **inline comments** while its review *body* is a boilerplate template
with no content. Copilot additionally hides real findings inside a collapsed
`<details>Suppressed comments</details>` block.

**Never conclude "no findings" from an empty review body.** One batch was read body-only across six
PRs and reported clean; it was carrying 5 P1s and 8 P2s in inline comments, unread for two days,
including an agent-readable signing key and a migration that silently reopened an auth gate on
upgrade. Read the suppressed and low-confidence comments too — several were the best findings in
that batch.

**An unresolved thread re-anchors to the current head, so `commit_id` does not mean "reviewed
this commit".** After a push, an open review comment from three commits ago comes back with
`commit_id` equal to the new head while `original_commit_id` still points at the commit it was
actually written against. Filtering inline comments by `commit_id == HEAD` therefore reports stale
findings as fresh ones — and, worse, makes an absent re-review look like a delivered one. Establish
whether a reviewer has actually re-reviewed from the **review** record, not the comment records:

```bash
gh api repos/{owner}/{repo}/pulls/<n>/reviews \
  --jq '.[] | "\(.user.login) reviewed \(.commit_id[0:10])"'
```

(Observed on this template's own PR #5: four findings appeared to be current at the new head, all
four were round-one threads re-anchored, and the reviewer had not re-reviewed at all.)

Two more harvest traps:

- **A failed bot run looks like a clean one.** A Copilot error lands as a `COMMENTED` review whose
  body says it could not review, with zero inline comments; `gh pr view --json reviews` renders that
  identically to a real pass. Check body content and inline count.
- **Filter agent-posted bookkeeping replies out of the review record.** They post under the
  operator's credentials and masquerade as human reviews. Distinguish by `in_reply_to_id`, not
  author.

## 3. Triage by verifying

A finding is a claim, not a verdict — whether it came from a bot, a human, or your own earlier
session. Trace or reproduce it before acting.

Four questions that decide most findings, each of which has changed a real verdict:

- **Is the flagged path reachable?** A finding can be correct about the external world and still
  irrelevant, because this codebase cannot produce the input it describes. Guarding an unreachable
  path costs maintenance and buys nothing. (A reviewer correctly noted a CLI rejects unknown
  categories; the category the client sends is a constant in its own template map. Declined.)
- **Which end is wrong?** When a comment reports code and documentation disagreeing, the
  documentation is often the side to fix. (A reviewer flagged a fake's error string as not matching
  the notes. The fake was right; the notes had dropped a prefix.)
- **What does the finding actually support?** A comment can be right that wording is misleading
  without being right that the underlying rule is wrong. Fix the wording, not the rule. (A reviewer
  read an invariant as condemning two existing inputs. The invariant was right; its phrasing
  invited an unnecessary refactor.)
- **What context did the reviewer have?** A bot reviewing one PR cannot see a decision made three
  PRs earlier. Severity badges and assertive phrasing do not encode that missing context — weigh
  the finding against what the reviewer could see.

Sort each into: **fix now** / **defer to the tracker** / **decline with a stated reason**.

**Declining has to actually happen.** A round that accepts every finding is a warning sign, not a
good score. Reject anything that violates `AGENTS.md`, and say why — automated reviewers read that
file too.

Deferrals go to the repo's external tracker, never a PR-body table: a triage table goes stale
between rounds and vanishes on merge.

## 4. The round cap

**Three reactive rounds, then defer.** Past the cap, remaining findings are ticketed, not patched.

**If one small change draws three or more findings, revert it and ticket it** — it needs design
time, not another patch.

**The cap can arrive early.** When a round's findings are all in code the previous round wrote,
that is the cap arriving — regardless of the round count or how few files are in play. Three rounds
is a ceiling, not an allowance.

The cap exists because rounds introduce bugs. A measured batch ran seven reactive rounds with
finding counts 6 → 2 → 1 → 3 → 2 → 5 — not convergence: four of the five later rounds were fixing
defects a previous round's own fix had introduced. A separate repo independently recorded a "third
instance in this batch of a fix introducing the next round's finding" the same week. The PR that
added this very paragraph did it again: its round-2 fix introduced a round-3 P1 (a disposition
check written severity-blind, so a ticketed P1 would have satisfied the rule meant to block it),
and a second round-2 fix turned out not to work at all. Three rounds is a ceiling for a reason,
and docs-only changes are not exempt.

**Hitting the cap is not the same as stopping mid-air — exit through a declared closing round.**
The cap says stop *patching*, not stop *thinking*: when it fires (at three rounds, or early by the
rule above) with findings still open, say so in the PR and name that round the closing one. In it,
only P1/red blocks; everything else is ticketed with the review link, and nothing new is started.
This is an exit procedure, not another lap — an agent that reads the cap as "abandon the open
findings" leaves accepted work untracked on trunk, which is the failure the followup pre-flight
in `batch-review` Phase 7 exists to catch. (A sibling repo's six-round series landed this way. Its
own retrospective is the cheaper lesson: a schema-driven feature that draws that many rounds
needed a design pass up front, per the revert-and-ticket rule above.)

## 5. Never trust a green signal

Every one of these has produced a false clear for real:

| Signal | How it lies | The check that catches it |
|---|---|---|
| "Zero unresolved threads" | Reads identically whether feedback was addressed or never solicited | Verify a review was actually requested and delivered **for the current head commit** |
| "No new comments" | Suppressed/collapsed blocks hide real findings under a clean summary | Expand and read the raw review payload via `gh api`, not the summary state |
| New test passing on first run | May be passing against unfixed code | Revert-and-confirm: watch it fail without the fix |
| Green CI run | Path filtering may have skipped the half of the suite your change lives in | Check **which jobs actually ran**, not the rollup colour |
| A merge command that returned | Stacked merges are asynchronous; rules are evaluated when the merge runs | Poll to a terminal status; `enqueued` is not `merged` |
| A delivered review, after a late fix | The review is real, but describes an earlier commit — "reviewed and approved" quietly becomes "approved something else" | Require *some* successfully-delivered review whose `commit_id` equals the current head SHA (§6) |

Two related habits:

- **Inference from a plausible nearby cause is not diagnosis.** During a platform outage, a red main
  was attributed to the outage's known error; the outage was real but unrelated, and the actual job
  failure had been there all along. Read the actual failure.
- **Verify propagated fixes by content** (grep, or run the test), never by `--stat`.

## 6. Merging

**Before merging, confirm the head you are merging is the head that was reviewed.** A go-ahead is
given at a moment; any fix after it moves the branch past what anyone looked at.

The criterion is not "the latest review looks recent" — review ordering is not reliable, and a
*failed* bot run also lands as a `COMMENTED` review carrying the current head's `commit_id` (§2).
Matching on SHA alone would therefore clear precisely the unreviewed merge this check exists to
catch. Require **at least one successfully-delivered review whose `commit_id` equals the current
head SHA**, after applying §2's failure and bookkeeping filters:

```bash
HEAD_SHA=$(gh pr view <n> --json headRefOid --jq '.headRefOid') \
gh api --paginate repos/{owner}/{repo}/pulls/<n>/reviews \
  --jq '.[] | select(.commit_id == $ENV.HEAD_SHA)
       | select(.state != "COMMENTED" or (.body|length) > 0)
       | select(.body | test("unable to review|encountered an error") | not)
       | "\((.user.login // "unknown")) \(.state) bodylen=\(.body|length)"'
```

**A review record is evidence, not proof — the output still has to be read.** Two artifact classes
masquerade as reviews and both are filtered above, but neither filter is airtight:

- *Bookkeeping* — empty-bodied `COMMENTED` records the agent's own thread replies create (below).
- *Failed runs* — a bot that errored posts a short `COMMENTED` body saying so, at the current head.
  Caught here by matching its message text, which is brittle: the wording is the vendor's to change.
  The durable corroboration is **zero inline comments at that commit** (§2), so when a row looks
  thin, check the inline count before counting it.

This exact case is why the filter exists rather than being a footnote: while this check was being
written, Copilot errored on the very PR that adds it, and the 117-byte "encountered an error"
notice sailed through an earlier version of the filter as a delivered review.

**Your own thread replies land in this list.** Every `gh api .../comments/<id>/replies` call creates
an empty-bodied `COMMENTED` **review** record under the operator's account, stamped at the current
head. An agent that has just replied to and resolved a round of feedback will therefore find its own
footprints sitting exactly where a review should be, and read them as one. (Measured on this
template's own PR: seven replies produced seven such records, timestamps matching to the second.)
The filter above drops them by their exact signature — `COMMENTED` *and* empty — rather than by
emptiness alone. That distinction matters: GitHub permits an `APPROVED` review with no body, so
filtering on emptiness alone would discard a genuine current-head approval and report an approved
head as unreviewed.

Two limits worth knowing before you rely on the result:

- A reviewer who left **only inline comments** and no summary is dropped by the filter — and
  **inline comments cannot rescue the check.** §2 establishes that an unresolved comment's
  `commit_id` is rewritten to the current head while `original_commit_id` keeps the truth, so
  "inline comments exist at the head" is not evidence the head was reviewed. It is the *same* false
  clear this gate exists to prevent, re-entered through the back door. (Measured on this template's
  PR #5: seven comments report a `commit_id` they were never written against — one written against
  `d0e8ade` now reports `db8c586`.) Either bind each comment back through `pull_request_review_id`
  and validate *that review record's* commit, or — simpler, and the better default — treat the empty
  result as **"review of this head could not be established"** and disclose it. Never upgrade an
  absence into a pass.
- A reviewer that only ever posts **issue comments** — §1 notes these exist, and they never create
  review records at all — cannot satisfy this gate in either direction, because an issue comment
  carries no commit binding. If a repo's roster is issue-comment-only, this check cannot be made to
  pass mechanically: say so to the operator and let the merge decision rest on an explicit
  disclosure, rather than reading the permanent empty result as either a pass or a failure.

Three details that each cost a wrong answer if dropped. **`--paginate`** — reviews are paged at 30,
so without it a matching review on a later page reads as empty, and this check's empty output means
"unreviewed". **The environment variable** — `gh api --jq` takes only a filter and rejects jq's
`--arg` with "accepts 1 arg(s)" (piping to a standalone `jq -r --arg h "$HEAD_SHA"` works instead,
where `jq` is installed). **The `// "unknown"` fallback** — `.user.login` is null for a deleted
account, which otherwise errors the whole filter and takes the check down with it.

Empty output means **review of this head could not be established** — not that it was definitely
never reviewed; §1's issue-comment-only reviewers and inline-only reviews both produce an empty
result while a review exists. Report the limit, not a stronger claim than the evidence carries.
Output alone is not sufficient either: check each row is a real
review rather than an error notice (§2 — a failed run says it could not review and carries zero
inline comments) or an agent bookkeeping reply. If nothing valid matches, either solicit a review of
the current head or **tell the operator plainly that the delta is unreviewed** — the authorization
may well still stand, but it should be given in view of that, not around it.

Evidence, from a sibling repo's own history: the operator's go-ahead was followed by three
further commits of fixes, and the commit that actually merged had been reviewed by neither bot.
The merge was authorized and the delta was deletions-only, so the outcome was fine — but the
report implied a review that had not happened, and the only reason anyone noticed was a
background job that had already been written off as superseded.

The merge gate itself lives in `AGENTS.md` §8 — **read it there before merging anything.** That file
is canonical for the gate, exactly as this one is canonical for the mechanics above. The one-line
orientation is that the baseline is a live operator go-ahead and that agents must not propose loosening it; every
qualification on that — what a conditional grant is, what a session carve-out covers, what does not
count — is in §8 and is deliberately not duplicated here.

## 7. Mechanical gotchas

Small, recurring, and each one wastes a round when it bites:

- **Replying to a review thread does not resolve it.** Resolution is the `resolveReviewThread`
  GraphQL mutation, and the thread ID must go in as a bound variable
  (`gh api graphql -F threadId=...`), not string-interpolated into the query — the interpolated
  form fails with "malformed" but can *look* like it worked.
- **`gh api -f in_reply_to=<id>` 422s.** That field is numeric: use `-F` (typed), not `-f`
  (string).
- **Multi-line commit messages go through `git commit -F <file>`, never inline `-m "..."`.** An
  embedded quote closes the shell string early and the remainder leaks as positional args,
  surfacing as a baffling `error: pathspec 'X' did not match any file(s)` that says nothing about
  quoting.
- **`git merge-base --is-ancestor` cannot tell you whether work landed via a squash merge.** The
  squash is a new commit with no ancestry relationship to the branch's commits — check by content
  (diff the files, grep for the change), not by ancestry.

---

**Maintenance note.** This file is the canonical copy. The account-wide standalone
`~/.claude/skills/batch-review/SKILL.md` deliberately carries its own self-contained duplicate,
because it runs in repos that have no `AGENTS.md` of this shape and no `docs/` — it is
regenerated from the shared template's `batch-review`, so changes here reach it by regeneration,
not by reference. That copy is expected to lag; check it when this file changes materially.
