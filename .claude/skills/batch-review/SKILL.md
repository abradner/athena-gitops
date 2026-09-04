---
name: batch-review
description: Batch PR shipping workflow - fan out a body of work as a stack of small atomic single-commit PRs (interstitials, from base to cap), let CI and automated reviewers run immediately but treat all feedback as write-only until the whole batch is in, then synthesise the feedback in aggregate, ship all reactive work as ONE followup PR stacked on the cap, resolve interstitial comments as "fixed in the followup" or "not relevant", then land the train — one atomic `gh stack merge` on GitHub stacked PRs (two steps when the followup stayed loose on the cap), or bottom-up merges in the manual fallback flavour. Use this whenever the user wants to review open PRs holistically or in aggregate, process accumulated bot/agent review comments across several PRs, ship a "review feedback batch" or followup PR, or mentions their overnight/fired-off batch of work - even if they don't say "batch review" explicitly. ALSO use at fan-out time, when the user asks to build a planned body of work as small/carefully-factored/stacked PRs. This is a deliberately different, opt-in mode for a genuine multi-PR fan-out, not this repo's default single-PR flow.
---

# Batch review

The core bargain: **feedback is write-only until the whole batch is synthesised.** CI and the bots
run immediately on every PR and comments accumulate freely — but nothing responds to them and no
reviewed branch is rewritten until the synthesis pass. All reactive work ships as one followup PR.

The mechanics of getting a PR actually reviewed and actually verified — reviewer triggers, the
three-surface harvest, triage, the round cap, the green-signal traps — are **shared with the
single-PR flow and live in `docs/pr-review-machinery.md`. Read that first.** This skill covers only
what batching changes.

Why: reacting piecemeal across several in-flight PRs creates real churn — branches rewritten under
open reviews, repeated rebase/conflict cycles, reviewer attention fragmented across many small
live threads instead of one synthesis pass. The default single-PR flow (react immediately, merge
when green) remains correct for one PR at a time; this skill exists for the multi-PR case only.

## Terminology

| Term | Meaning |
|---|---|
| Interstitial | A single-commit PR in the proactive stack |
| Base / Cap | The bottom-most / top-most interstitial |
| Followup | The one N-commit reactive PR stacked on the cap — the batch's only live-feedback surface and its release gate |
| Stack object | The stack entity on GitHub (stacked flavour only) — GA, addressed by stack number |

## Repo specifics

- **Validation commands**: `./scripts/validate_yaml.sh` (needs `yq` 4, pinned in `mise.toml`) is
  the local pre-push gate — the same command the `yaml-syntax` CI job and the local pre-commit
  hook both run. CI additionally runs `kubeconform` (k8s-schema job, against `cluster/`) and a
  helm-render diff job; neither has a local mise-managed equivalent, so treat their CI result as
  authoritative rather than trying to reproduce them locally.
- **Bot roster**: see `docs/pr-review-machinery.md` §1 for the live record. Copilot confirmed
  reviewing every recent human PR checked; Codex unconfirmed (no evidence either way — this repo
  has never invoked it, not a claim that it wouldn't work).
- **Merge strategy**: **squash**, confirmed from repo history — recent merges are single-parent
  commits carrying the classic GitHub squash format (`Title (#N)`), and this repo's GitHub
  settings permit squash/merge-commit/rebase all three, so this is observed convention, not a
  platform constraint. No stacked or multi-PR batch has run here yet, so there is no established
  interstitial-vs-followup split to record — when the first one does, note here whether it
  diverges from squash-throughout. Several rules below are strategy-dependent
  and are marked `[SQUASH]` / `[MERGE-COMMIT]` — keep the matching variant, delete the other.
  Porting these rules across strategies unexamined has produced subtly wrong instructions that
  only surfaced under real conflict pressure, three separate times in one sibling repo.
  If interstitials and the followup use *different* methods, say so explicitly: under stacked
  flavour one `merge_method` covers the whole atomic group, so a mixed convention costs a second
  checkout-then-merge cycle (Phase 7) and an agent that doesn't know will collapse the distinction.
  *Why the distinction bites:* squash-merging a parent can break its children. The squash writes a
  **new** commit duplicating the parent's payload while the child's merge base stays pre-batch. Git
  still recognises identical changes on both sides, so a child carrying *only* the parent's payload
  merges cleanly — the conflict comes from **divergence**, where the child also modified the same
  hunks and git has two different versions from an ancestor with neither. Predict conflicts per
  overlapping-and-modified *hunk*, not per shared file. Merge-commit interstitials avoid this
  because child history stays a true ancestor.

  **The rule this implies for manual flavour: by default, only the top PR — the one nothing else
  depends on — is squash-safe.** Everything below it merges `[MERGE-COMMIT]`, or the next squash
  in the chain hits the divergence above. Squashing a lower PR is still possible, but only by
  paying the cost already named for a main-moved or showstopper interstitial: restart every
  affected child from fresh post-squash main rather than continuing to stack it on pre-squash
  history — a real ongoing cost, not a one-time exception. `gh stack merge` needs none of this:
  its squash path rebases each remaining PR `--onto` the post-squash parent automatically (see
  Merging — stacked flavour below), so a stacked train can squash top to bottom throughout.
- **Deferral convention**: GitHub Issues on this repo. Never a PR-body table — it goes stale
  between rounds and vanishes on merge.

## Phase 1 — Flavour probe

Stacked PRs are GA. **If the remote is GitHub, assume stacked flavour** and probe only to confirm
the tooling is present and the repo is enabled:

```bash
gh stack --version
rc=0; gh stack view --json || rc=$?; echo "exit=$rc"
```

**Capture the code with `||`, not with a `;`-separated `rc=$?`.** A non-zero exit is the
*expected* result here, so under `set -e` anything outside a condition context aborts the shell
before it can report the very code the table below turns on — `cmd; rc=$?` then prints nothing at
all, and the probe looks like it produced no answer rather than a meaningful one. Verified:
`bash -c 'set -e; false; rc=$?; echo "exit=$rc"'` exits 1 silently, while
`bash -c 'set -e; rc=0; false || rc=$?; echo "exit=$rc"'` prints `exit=1`. Without `set -e` both
forms work, which is why the broken one survives casual testing.

**Exit 2 is the expected answer before fan-out**, not a failure: it means "no local stack yet",
which is exactly true of a branch you are about to build a batch from. Do not read it as
unavailability. Only these distinguish the outcomes:

| Exit | Meaning at Phase 1 |
|---|---|
| `2` | Not in a stack yet — normal pre-fan-out. Stacked flavour is available; proceed. |
| `0` | Already in a stack (resuming a batch). Proceed. |
| `9` | Stacked PRs **not enabled for this repo** — the one genuine fallback signal. |

Two different outcomes, and conflating them is how a batch quietly runs on the worse path:

**Stop and tell the operator** — the environment is fixable, so fixing it beats working around it:

| Condition | How it shows up | Say |
|---|---|---|
| `gh stack` not installed | `unknown command "stack"` | `gh extension install github/gh-stack` |
| Extension below the floor | no `merge` subcommand — it landed in **v0.1.0** | `gh extension upgrade stack` |
| `gh` itself too old | `gh stack` needs gh **v2.0+**; `gh skill` needs a recent build | upgrade `gh` |

**Fall back to manual flavour** — genuinely unavailable, nothing the operator can install:

| Condition | How it shows up |
|---|---|
| Not a GitHub remote | GHE below the feature, or any non-GitHub host |
| Stacked PRs not enabled for the repo | `gh stack` **exit code 9** |
| Batch exceeds the platform cap | a stack holds at most **100 PRs** |
| Stacked tooling misbehaving mid-batch | see the trust note below |

Record whichever applies in the batch's tracking note.

A batch normally finishes in the flavour it started. Both mid-batch transitions are possible —
stacked → manual via `gh stack unstack` is the routine degrade path (available any time
adoption misbehaves, no reason needed beyond that); manual → stacked is a judgment call, not an
escape hatch, and needs a real reason to hold — e.g. an interstitial has grown to several
commits, so the manual flavour's payoff (a merge commit wrapping exactly one reviewed commit) is
already gone and the stacked squash train is strictly easier to land. Adopt with **`gh stack link
<branch|pr>...`** (bottom to top), not `gh stack init --adopt`: `link` operates GitHub-side on
the PRs as they already are, rewrites no local tracking state, and leaves branch SHAs and
existing review context untouched. Verify afterwards that each PR's base is unchanged and no
branch was rewritten, then update every Batch block (flavour, the reason, the selected merge
control). Note that **merged and queued PRs cannot be unstacked** — once the train starts, that
escape hatch is gone for the PRs already through it, regardless of direction.

**If a specific transition attempt fails partway** (an unstack or a link that errors, leaves a
PR's base pointed somewhere unexpected, or a branch that doesn't verify clean afterward): stop,
record *why* it failed, and signal the operator explicitly rather than retrying or picking a
recovery path unprompted. If there is any risk the failure left commits, a base, or a branch in
a state that could lose or duplicate work, ask for the path forward before taking any further
action — this is not a case where a plausible-looking recovery is safe to attempt solo.

Treat stacked-flavour tooling claims as recorded observation, not guarantee. Sibling repos observed
`gh stack sync` reporting "✓ synced" without pushing, and stack commands silently operating against
a stale local main ref inside worktrees while printing success — both classes were acknowledged and
fixed upstream in v0.1.0 (stale-trunk rebase, and amended parent commits replaying into children),
which is itself the reason to pin the version you are reasoning about. `gh stack push` is a live
example of the same hazard: v0.0.8's own `--help` claims `--atomic` all-or-nothing pushes while the
v0.1.0 reference documents it as non-atomic per-branch leases. Verify the branches moved; don't
read the success line.

## `gh stack` — operational reference (stacked flavour)

GitHub ships an upstream agent skill for the CLI, published from the `gh-stack` repo's own
`skills/` directory. Install it per that repo's current instructions rather than from a command
memorised here — `gh skill` is new, its flags differ by `gh` version, and it defaults to an agent
target you may not want. Treat the installed skill as authoritative for the full command surface, exit-code recovery, and stack design. What follows is
only the subset a batch trips over, kept here because the failures are silent and mid-train.

**The TTY trap.** `gh stack` branches on whether stdout is a TTY: piped, most commands print static
text; under a PTY the same command opens a wizard or a full-screen TUI and **blocks forever**.
Agent harnesses differ in which they present, so never rely on the detection — always pass the flag.

| Always | Never bare | Why |
|---|---|---|
| `gh stack view --json` | `gh stack view` | TUI under a PTY |
| `gh stack submit --auto` | `gh stack submit` | prompts per new PR |
| `gh stack checkout <pr-url>` then argument-less `gh stack merge --yes` plus one explicit method flag | `gh stack merge <number>`, `gh pr merge` | `gh pr merge` cannot merge a stack; a bare number resolves stack-number-first and can land a different train (Phase 7); no flags at all opens a wizard and reuses the last-used method |
| `gh stack checkout <target>` | `gh stack checkout` | selection menu |
| `gh stack up`/`down`/`top`/`bottom` | `gh stack switch`, `gh stack modify` | menu/TUI-only, no non-interactive path |

**Reading state.** `gh stack view --json` writes JSON to stdout; status messages go to stderr —
parse the former, branch on exit codes, never scrape the latter. Fields: `trunk`, `currentBranch`,
`branches[]` of `name/head/base/isCurrent/isMerged/isQueued/needsRebase`, plus `branches[].pr` of
`number/url/state` (absent when no PR exists). `base` is the *last known* parent SHA, not the
parent's current tip; `needsRebase` is the real signal.

**Exit codes that change what a batch does:** `9` stacked PRs unavailable → fall back to manual
flavour (Phase 1); `3` rebase conflict → `gh stack rebase --continue` after resolving, or `--abort`;
`8` stack file locked by another process → retry after ~5s, don't escalate; `2` not in a stack;
`7` rebase already in progress. `0` is success, `1` generic — read stderr.

**Multiple remotes:** `push`, `submit`, `sync`, `rebase`, and `link` need `--remote <name>` unless
`remote.pushDefault` is set; `checkout` and `trunk` have no `--remote` flag and require the config.

**Driving the merge from the API** (for automation and scripted runners — *not* as a way around an
outdated extension; that is a version floor, and Phase 1 says interrupt rather than hand-roll it).
`gh stack merge`
is a wrapper over the async merge API, which is the *only* supported way to merge a stacked PR:

```
# submit — 202 with status "pending" and a uuid; 200 "merged" if already merged;
# 400 "failed" if closed/draft; 409 if a request already exists (returns THAT uuid,
# whose options may differ from the ones you just asked for)
echo '{"merge_method":"squash","merge_action":"default"}' \
  | gh api --method PUT repos/OWNER/REPO/pulls/<n>/merge-async --input -

# poll ~1/s until status != pending; a valid lookup is always 200
gh api repos/OWNER/REPO/pulls/<n>/merge-async/<uuid>
```

`status` is `pending` | `merged` (`details.sha` is the merge commit) | `enqueued` | `failed`.
Optional body fields: `merge_method`, `merge_action` (`default` recommended — it picks direct
merge or the queue), `commit_title`, `commit_message`, `sha` (head-match guard; use it when the
branch could have moved under you). `commit_title`/`commit_message`/`merge_method` are **ignored on
merge-queue actions**. Results expire **24 hours** after their last update, then the uuid 404s — a
poll loop that outlives that window cannot distinguish "expired" from "never existed".

## Phase 2 — Self-review before fan-out

Before any PR opens, run one pointed self-review question per branch — "what did this change make
newly risky?" — delegated to a cheap review subagent, findings fixed in the commit itself.
Evidence for why this is worth the cost: the same finding surfaced before opening costs a
`git commit --amend`; the same finding after fan-out costs a reactive round, and rounds introduce
bugs (see Provenance).

When more than one branch in the batch touches the same code or shared surface — or any branch
went through an agent-performed merge or rebase — also run
`.claude/skills/stack-integration-check` here, on the combination. The trigger is the shared
surface, not how many agents were involved: one agent writing two branches against the same module
forks it just as easily. Per-branch self-review inherits per-branch review's blindness: a semantic
fork or a silently deleted control between two individually-green branches is invisible to both,
and it is a one-commit fix now versus published-history archaeology after fan-out.

## Phase 3 — Fan-out

- **Chain the PRs into a linear stack even when the branches look independent.** Never fan out as
  a DAG of siblings off trunk. Sibling PRs each run CI against trunk alone, so *nothing ever tests
  the combination*: one measured batch shipped two branches that were green individually and did
  not compile together (a rename landing under another branch's new call site), plus six tests that
  failed only in the union — both found by hand, and every sibling then had to re-resolve the same
  conflicts individually after review had finished. A linear stack runs each PR's CI against its
  parents, so both would have failed a check instead.
- One commit per interstitial, opened ready-for-review immediately (trips auto-reviewers once,
  early, while reaction is still cheap to withhold). Stacked flavour:
  `gh stack submit --auto --open` — `--auto` skips the per-PR title editor, `--open` is what makes
  them ready rather than draft. Titles and bodies are auto-generated, so follow with `gh pr edit`
  to install the batch block below; don't hand-open the PRs to get around it.
- Every PR body carries a machine-scannable batch block, so the ground rules survive even when
  this skill doesn't trigger and AGENTS.md goes unread:

  ```
  ## Batch
  Batch: <name> | Flavour: manual|stacked | Position: N of M
  Stacked on: #<parent> | Feedback: write-only until synthesis — see followup PR
  Merge: <stacked: one atomic `gh stack merge` on the checked-out train | stacked, loose followup: stack merge, then the followup on the updated base — two steps | manual: bottom-up> after followup approval, operator-gated
  ```

- Never guess PR numbers; never leave placeholder references live.
- **Neighbouring-PR overlap sweep**: intersect the batch's touched files against every other open
  PR (`gh pr list`, then `gh pr view <n> --json files`). Disposition per overlap, recorded:
  **comment/adapt-after** (default — tell the neighbour what changed and to adapt once the train
  lands; never absorb its work, never pre-emptively rebase its branch), **adapt-in** (only when the
  batch semantically breaks it and the fix is mechanical — lands in the followup, attributed), or
  **escalate** (irreversible collisions, e.g. competing migration timestamps, are operator
  decisions). Re-run the sweep at readiness — parallel work opens while a batch bakes — and name
  every neighbour that must adapt in the readiness report.

## Phase 4 — Bake

Interstitial CI red is tolerated (the followup fixes it). Exactly one showstopper bar justifies
touching an interstitial mid-bake: **an irreversible action on merge** — a destructive migration,
an unrecallable external side effect. Everything else waits.

- **Stacked flavour**: `gh stack sync` fetches, cascade-rebases each branch onto its updated
  parent, and pushes — this is the supported answer to both "main moved" and "a fix landed low in
  the stack", and it supersedes the manual rules below. Two cautions: sync **never opens PRs**
  (that is `submit`), and on local/remote divergence it prints both chains, changes nothing, and
  **exits 0** with "Sync aborted" — a success exit code that means it did not sync. Check for the
  abort, not the exit status. On exit 3, the stack has already been restored; run
  `gh stack rebase` to recreate the conflict, resolve, then `--continue`.
- **Manual flavour**, if main moves under the stack: `[MERGE-COMMIT]` merge main up through the
  stack by hand, never rebase a reviewed branch. `[SQUASH]` restart affected branches from fresh
  main after their parents land; never stack new commits on pre-merge history.
- **Manual flavour**, showstopper fix injected low in the stack: `[MERGE-COMMIT]` propagates
  upward through the child merges. `[SQUASH]` must be explicitly propagated into each child —
  squash does not preserve the ancestry that would reunify it. (Stacked flavour handles this case:
  its squash path uses `git rebase --onto` so commits that vanish in the squash don't resurface as
  artificial conflicts in the children.)

**"No checks at all" satisfies the CI condition.** Where workflows are `paths`-filtered, a PR
touching only docs, skills or CI config legitimately runs *nothing*. Check whether any workflow's
filters match the PR's paths; if none do, that half of the gate is already met. A naive
"wait until all checks conclude" gate waits forever on exactly the PRs a batch produces most of.

**Driving phase transitions**: subscribe to PR/CI events and react when they arrive; pair the
subscription with a bounded fallback timer (order of ~30 min for a full stack's reviews, ~10 min
for a single re-solicited review) so a bot no-show can't stall an unattended batch. That fallback
is this workflow's single named exception to event-driven monitoring, not a license for scheduled
polling elsewhere.

## Phase 5 — Synthesis

- **Harvest all three surfaces on every PR**, per `docs/pr-review-machinery.md` — the findings are
  usually inline, an empty review body is not "no findings", and a failed bot run renders like a
  clean pass. At batch scale this is N times the surface, and the batch that was read body-only and
  declared clean was carrying 5 P1s and 8 P2s across six PRs.
- Triage by verifying (shared doc §3). Batch-specific sorting: fix in followup / defer to tracker /
  decline with stated reason — nothing is fixed in the interstitial it was found on.
- Add one aggregate pass across the full stack diff — cross-PR interactions are structurally
  invisible to per-PR reviewers. **When implementation was delegated to subagents, this pass must
  itself be delegated to one independent fresh-eyes reviewer agent**: the orchestrating session
  has read agent reports and bot comments, not the diff — its "own review" is process review
  wearing a code-review hat, and nobody has held the whole change. Brief the reviewer with the
  repo's trust-boundary questions and instruct it to *trace* claims, never trust comments, PR
  bodies, or the orchestrator's summary. Launch it in parallel with the comment harvest so its
  findings fold into the followup's first commit instead of burning a late reactive round; its
  findings count toward the round cap like anyone else's. Steps 2–4 of
  `.claude/skills/stack-integration-check` (justify deletions against the branch below, compare
  shared-surface implementations directly, check both artifacts) are the checklist for the
  cross-PR half of this pass.
- The reviewer's diff command is `git fetch origin <default-branch> && git diff origin/<default-branch>...<followup-branch>` (or the cap branch
  before the followup exists) — spell out the left side and keep the command on one physical
  line: `git diff ...<branch>` is *valid* git that silently defaults the left side to HEAD, so a
  checkout not on main yields a quietly truncated diff, and a fresh-eyes verdict over a truncated
  diff is a confident "cleared" over code nobody read. (A wrapped command has the same failure:
  the first pasted line, bare `git diff`, is silently valid too.)

## Phase 6 — The followup PR

- Open as a **draft targeting main** so its diff is the whole stack — spend one budgeted
  aggregate bot review there — then **retarget to the cap** before marking ready, so per-PR
  reviewers see only the reactive delta. Stacked flavour: do this *before* adding the followup to
  the stack, then append it at the top. **Adopt from the cap; do not submit from the followup** —
  the verified sequence is:

  ```bash
  git checkout <cap-branch>
  gh stack add <followup-branch>   # "✓ Adopted existing branch ..."; the SHA is unchanged
  gh stack submit --auto           # "PR #N ... is up to date" — no duplicate PR is opened
  ```

  Running `gh stack submit` from the followup's *own* branch fails with
  `✗ current branch "<branch>" is not part of a stack`. And do not hand-edit the base with
  `gh pr edit` on a PR the stack already owns: local tracking and GitHub go out of step, and the
  next `sync` reports a divergence instead of syncing.
- Maintain a `## Review focus` section in the body, restated each round.
- **The three-round cap applies to the followup as a whole** (shared doc §4), not per interstitial:
  the followup is the batch's single reactive surface, so its third round is the batch's last —
  and the shared doc's early-arrival rule applies to it as one unit. When the cap fires with
  findings still open, close the followup out through the shared doc's declared closing round
  (P1/red blocks, everything else ticketed) rather than merging over them: the Phase 7 pre-flight
  below is what catches a train that skipped this.
- **Never trust an aggregate review signal.** The green-signal table in `docs/pr-review-machinery.md`
  applies unchanged; two entries bite hardest at batch scale. "Zero unresolved threads" reads
  identically across a whole stack whether feedback was addressed or never solicited — so verify
  review was delivered for each PR's *current* head, not that the stack looks quiet. And a green
  rollup over N PRs hides which jobs ran on which: path filtering that skipped a suite on one
  interstitial is invisible in the aggregate.

## Phase 7 — Merge the stack

- **Operator gate: never start this phase without the operator explicitly saying to merge now.**
  A synthesis pass, a green followup, and an auto-mode session default are not that signal. If the
  stack is ready, say so and stop. A conditional go-ahead that names this batch and states its
  condition *does* count, and covers merging unattended; an ambiguous "carry on" does not. Every
  new session resets to the manual gate — see `AGENTS.md` §8, and don't propose loosening it.
- **Pre-flight, first: confirm the head each PR is merging is the head that was reviewed** — the
  procedure, and what to do when they differ, is `docs/pr-review-machinery.md` §6.
  Disclose an unreviewed delta rather than letting the approval imply a review that never happened.
- **Pre-flight, second: account for the followup.** The write-only bargain is that feedback gets
  answered *somewhere*, and nothing in the mechanics enforces it — a stack merges perfectly well
  without a followup and the merge control does not ask. Merge without one and every accepted
  finding silently becomes debt on trunk with nothing tracking it.

  A followup is not mandatory; *accounting* for its absence is. If one exists, it is in one of two
  legitimate positions — confirm which, then go on to the next check:

  - **In the train** (the normal stacked case) — it merges with everything below it in one atomic
    operation. Confirm it appears in `gh stack view --json`.
  - **Loose, targeting the cap** — the documented degrade path when adoption misbehaves. This is
    legitimate, *not* a failed pre-flight: merge the stack first, then merge the followup manually
    on the now-updated base. Confirm its base is the cap, and say in the readiness report that the
    train takes two steps rather than one.

  Requiring every followup to be *in* the train would make that fallback unreachable, which is the
  opposite of what this check is for — it exists to stop a train merging with its feedback
  unanswered, not to mandate one mechanism for answering it.

  If none exists, establish which case this is:

  - **Synthesis has not happened.** This phase is premature. Say so and stop.
  - **Synthesis happened and produced nothing to action.** Legitimate — proceed. But confirm it
    rather than inferring it from quiet: "zero unresolved threads" reads identically to "never
    reviewed", so check that review was actually solicited and delivered for the current head.

  The failure this prevents is an agent finding no followup, assuming the second case because
  nothing looks wrong, and merging.
- **Pre-flight, third: confirm the followup's base is the cap, not trunk.** The Phase 6
  draft-first opening deliberately targets trunk for the aggregate review; if the retarget was
  missed, merging the followup collapses the whole stack into one commit.
- **Pre-flight, fourth: every accepted finding is dispositioned, and severity decides which
  dispositions are legal.** Walk the synthesis list:
  - **P1 / red: fixed, or declined in writing with the reason.** A ticket is *not* a valid
    disposition for a P1 — the shared doc's closing round says P1/red blocks, and a check that
    accepts "ticketed" for everything would let the train carry a verified P1 out to trunk under
    a rule written to stop exactly that.
  - **P2 and below: fixed, ticketed with the review link, or declined in writing.**

  This is the check that makes the followup's closing-round disposition real: the *second*
  pre-flight proves a followup exists, not that the findings inside it were dispositioned, and a
  followup carrying undispositioned findings passes every earlier check unchanged. Merging there is how
  accepted work becomes untracked debt on trunk — the same failure the second pre-flight was
  written for, arriving one level down.
### Merging — stacked flavour

**`gh pr merge` cannot merge a stacked PR.** The legacy synchronous merge endpoint and the
`mergePullRequest` GraphQL mutation are both rejected for PRs in a stack; the async merge API is
the only supported path. An agent that reaches for `gh pr merge` here is not slightly wrong, it is
blocked — and the failure arrives mid-train.

Merging a stacked PR lands **that PR and every unmerged PR below it**, atomically: the whole group
lands or none of it does. This retires the bottom-up walk. It also retires the
retarget-before-delete rule for stacked flavour specifically: on a partial merge the platform
retargets the lowest unmerged PR to the stack base and cascade-rebases automatically.

**Never pass a bare number to `merge`.** Per upstream, a bare number is treated *first as a stack
number, then as a pull request number* — and stacks and PRs are independent numbering sequences
over the same small integers (a sibling repo had stack **#46** live at a time when PR #46 did not
exist at all; the followup in that stack was PR #47). Whether a given number names both is chance,
and nothing about the number tells you. The failure mode is quiet and expensive: you pass the
followup's PR number, it happens to be a live stack number, and an atomic top-down merge lands a
different train than the one you checked. Nor can you probe your way out: `merge` takes integers
only (no URL, no branch), and `view` takes **no target at all** — it describes the *current local
stack*, so it cannot tell you what a bare number would have resolved to.

`gh stack checkout` *does* accept unambiguous targets (`<pr-url>` | `<branch>`), so name the train
there, verify what you landed on, then merge with **no argument**:

```bash
gh stack checkout https://github.com/{owner}/{repo}/pull/<followup>  # URL cannot be misread
gh stack view --json                                                 # now describes THIS stack
gh stack merge --yes --squash                                        # no argument = current stack
```

Use **two** checkout-then-merge cycles when the repo's interstitial and followup merge methods
differ — `merge_method` applies to the entire group, so a mixed convention cannot be expressed in
one call: first cycle on the cap's URL with the interstitial method, second on the followup's URL
with its method.

**The second cycle is derived, not observed — verify it before relying on it mid-train.** That
`merge_method` covers the whole group is documented; that the followup is still *stack-mergeable*
once everything below it has landed is an inference. After a partial merge the platform retargets
the lowest unmerged PR to the stack base and cascade-rebases, but a one-PR remnant may no longer
present as a stack, in which case the second cycle can fail — and `gh pr merge` is blocked for
stacked PRs, so the fallback is not obvious. Establish this on a low-stakes batch, not on the train
you care about. Each merge is separately atomic. Always pass `--yes` and an explicit method:
without `--yes` the command opens a wizard, and without a method flag it silently reuses your
last-used merge method — a default that is invisible, machine-local, and not what you want deciding
how a train lands.

### Merging — manual flavour

- Merge bottom-up. Before deleting a merged branch, verify every child PR has already been
  retargeted — deleting a base branch races the platform's auto-retarget and has closed child PRs
  mid-train. Retarget first, confirm, then delete.
- **Only the top PR — the one nothing else depends on — is squash-safe by default.** Squashing a
  PR with unmerged children writes a new commit duplicating its payload while their merge base
  stays pre-squash; git conflicts on any hunk they both touched (see Repo specifics above for the
  full mechanism). Everything below the top merges `[MERGE-COMMIT]`, unless the batch restarts
  affected children from fresh post-squash main instead of stacking on pre-squash history. This
  restriction is manual-flavour only — `gh stack merge` rebases each remaining PR onto the
  post-squash parent automatically, so a stacked train squashes top to bottom with no such cost.
- Followup merges last, squash-merged — this repo's observed convention, and always safe
  regardless since the followup is the top of the stack.
- If a child PR does get closed by a race, **judge whether restoring the base branch and
  reopening is the right call, or whether a fresh PR is** — it is not an automatic first step.
  `gh pr reopen` and `gh pr edit --base` both refuse a closed PR while its base branch is still
  gone, so restoring the base first is the load-bearing step when restoring is worth it:
  ```bash
  gh api -X POST repos/{owner}/{repo}/git/refs -f ref=refs/heads/BASE_BRANCH -f sha="$(gh pr view PARENT --json headRefOid -q .headRefOid)"
  gh api -X PATCH repos/{owner}/{repo}/pulls/CHILD -f state=open
  gh api -X PATCH repos/{owner}/{repo}/pulls/CHILD -f base=main
  gh api -X DELETE repos/{owner}/{repo}/git/refs/heads/BASE_BRANCH
  ```
  Reopening preserves the review thread trail, prior CI runs, and any linked discussion — worth
  it for a PR with real history on it. It costs an extra restore-then-delete round trip and needs
  the parent's last head SHA (`gh pr view PARENT --json headRefOid`) to still be resolvable. If
  that SHA is gone, or the situation doesn't call for the extra steps (a fresh child, a small
  fix, time pressure), open a *fresh* PR from the still-live head branch instead (the commits are
  intact, and CI results usually carry over), and let `Closes #N` in its body link the orphan —
  budget a new review round either way, since a fresh PR loses the original thread trail and even
  a restored one needs its own final check.
- Cheaper than the recovery: **drop `--delete-branch` until the whole stack has landed.** Deleting
  base branches is what starts this, and nothing needs them gone mid-train.

### Verifying the merge landed

The merge is asynchronous under both `gh stack merge` and the raw API, so **the command returning
is not the merge finishing** — this is the phase's version of "verify the output, not the
instrument."

- Branch protection and repository rules are evaluated **when the merge runs, not when it is
  submitted.** A submit that is accepted proves only that the PR is open and not a draft; a rule
  failure surfaces later as a `failed` poll result. Never report a merge from the submit.
- `enqueued` is a **terminal state for the merge request, not a merged state.** The stack went to
  the merge queue; the queue decides the outcome and may land the PRs in separate groups, and it
  picks the method, ignoring any flag you passed. Track the queue for the real result.
- `failed` means nothing merged — the operation is atomic, so there is no partial train to unpick.
  Read `details.message` for the cause before retrying.
- Auto-merge and rule bypass are **not available for stacked PRs**. `--admin`-style "just push it
  through" has no stacked equivalent; a blocked stack is blocked.

## Rules of thumb

One commit per interstitial. Open ready-for-review immediately. Nothing answers feedback until
synthesis. Showstopper bar = irreversible-on-merge only. One followup PR carries all reactive
work. Delegated implementation means delegated review — one fresh-eyes agent over the full,
spelled-out aggregate diff. Sweep neighbouring PRs at fan-out and readiness; comment, don't
absorb. Three reactive rounds, then defer. Verify aggregate signals; never trust the rollup.
Operator says "merge" — nothing else counts. Account for the followup before starting the train:
in it, loose on the cap (stack first, then the followup — two steps, and the readiness report says
so), or a confirmed nothing-to-action — and every accepted finding dispositioned before the train
moves: P1 fixed or declined in writing (never merely ticketed), P2-and-below fixed, ticketed, or
declined. Stacked: checkout the train by PR URL, then an
argument-less `gh stack merge --yes --<method>` lands everything below the top atomically — one
cycle per merge method, so a mixed interstitial/followup convention needs two. Never `gh pr merge`,
never a bare number, and a returned command is not a finished merge. Manual: bottom-up, retarget
before delete, only the top PR squash-safe by default. Tag once. A child closed by that race:
judge whether to restore the base branch and reopen it, or open a fresh PR — reopen refuses while
the base is gone, so restore it first if restoring is the right call.

## Where these rules came from

Ported across a fleet of sibling repos and corrected against real batches in each. Keep this
section when adapting: add to it rather than replacing it — the value is in the accumulated
evidence, not in any single batch.

- The write-only rule exists because piecemeal feedback batches once rewrote main under a
  still-open stacked PR and killed it.
- The three-round cap exists because a measured batch ran seven reactive rounds with finding
  counts 6 → 2 → 1 → 3 → 2 → 5 — not convergence: four of the five later rounds were fixing
  defects a previous round's own fix had introduced. A separate repo independently recorded a
  "third instance in this batch of a fix introducing the next round's finding" the same week.
- The self-review-before-fan-out gate exists because of the same data: findings are an order of
  magnitude cheaper before the stack exists.
- The bot-budget asymmetry (unrationed cheap reviewer, budgeted expensive one) comes from a
  measured split: the cheap bot caught 6/6 of a mechanical bug class; every expensive-bot finding
  that mattered was cross-file. Spend the budget on aggregate diffs.
- The followup-exists pre-flight was added after a nine-PR stacked batch merged its entire train
  with no followup at all. Thirteen accepted findings landed on trunk untracked, including a
  warning that could never fire (production never set the logger it wrote to) and a
  nondeterminism trap in a package documented as pure — a replay hazard in a workflow engine.
  Nothing was red: CI was green, every thread had been triaged, and the merge control asked no
  questions. The findings were recovered only because the synthesis notes happened to still be in
  a live session. Every other rule here assumes the followup lands; this is the one that checks.
- The retarget-before-delete rule exists because `gh pr merge --delete-branch` closed a child PR
  in a live merge train when branch deletion raced GitHub's auto-retarget. Two sibling repos have
  now hit it independently — once on a pair of PRs, once **twice in a single night** — and both
  times the reopen was attempted with the base branch still gone, so both concluded a closed
  child "cannot be recovered at all" and went to fresh PRs. A third repo's own history (a closed
  child on its own retarget race, reopened cleanly *after* restoring the base branch first)
  showed that conclusion was too broad: the base being gone, not the closure itself, is what
  `gh pr reopen`/`gh pr edit --base` refuse. The rule is corrected in place to a judgment call —
  restore-then-reopen when it's worth the extra round trip and the parent's last SHA is still
  resolvable, fresh PR otherwise — rather than quietly swapped, per AGENTS.md maintenance
  meta-rule 1. Don't delete branches mid-train remains the cheaper fix either way.
- The manual → stacked transition was one-way by default until a sibling repo's batch
  `op-contract-docs` (#5–#7) took the reverse deliberately: an interstitial had grown to several
  commits, so the manual flavour's whole payoff — a merge commit wrapping exactly one reviewed
  commit — was already gone, and switching to the stacked squash train cost nothing further.
  `gh stack link` (not `--adopt`) is the mechanism that repo used and verified safe: it operates
  on the PRs GitHub-side, as they already are, without rewriting local tracking state or branch
  SHAs. Corrected in place to a judgment call rather than an absolute, per AGENTS.md maintenance
  meta-rule 1 — with the guardrail that repo itself named: take this transition for a real
  structural reason, never to escape a problem, since that is what the one-way *stacked → manual*
  degrade already exists for.
- The only-the-top-PR-is-squash-safe rule makes explicit what the divergence mechanism (Repo
  specifics, above) already implied but never stated as a rule: raised when a sibling repo's own
  `[SQUASH]`/`[MERGE-COMMIT]` tagging was flagged as a *possible* instance of this confusion —
  unconfirmed which way that repo actually got it, but the choice clearly wasn't spelled out
  clearly enough for that question to be answerable at a glance. The mechanism was already
  documented; this names the conclusion it leads to, and the manual-flavour exception (restart
  children from fresh post-squash main) was already described for the main-moved and
  showstopper-fix cases — this just states it as the general rule those two are instances of.
- The independent fresh-eyes pass exists because a sibling repo's nine-PR delegated batch went
  through per-PR bot review on every PR, one aggregate bot review, and two orchestrator-run
  reactive rounds — and a fresh-eyes agent over the full diff then found what all of that missed:
  a reactive-round fix had silently defanged a security regression test (now passing without
  executing the branch it pinned), an unaudited denial path, and a privilege grant whose sole
  written justification was never verified under the live role. Three bot rounds: zero of the
  three. The same pass also *cleared* the batch's security claims by tracing them, which is what
  made the readiness report worth trusting.
- The spelled-out diff command exists because the shorthand `...<branch>` doesn't fail when
  copied — it silently diffs from HEAD, truncating the review surface — and the wrapped two-line
  form fails the same way (bare `git diff` is valid). Both were caught in review of the skill text
  itself before either bit a real batch.
- The overlap sweep exists because the same batch shared one file with a neighbouring open PR;
  one comment telling it to adapt after the train cost one API call and prevented a surprise
  conflict — and a parallel session honouring the don't-push-to-others'-branches rule the same
  night is what kept two sessions from colliding on one branch twice in a day.

- The stacked-by-default switch and the whole `gh stack` reference section came from a
  documentation and CLI audit (Aug 2026, `gh-stack` v0.1.0), **not** from a batch that went wrong —
  flagged as such so nobody cites it as incident evidence. What the audit found that contradicted
  this skill's own earlier text: stacked PRs left beta; `gh pr merge` is *rejected* for stacked PRs
  rather than merely awkward; merging is atomic top-down, which retires the bottom-up walk and the
  retarget-before-delete step for stacked flavour; and `gh stack merge` only exists from v0.1.0.
  The version pin matters — the machine this was written on had v0.0.8, which has no `merge` at all.
- The never-pass-a-bare-number merge procedure is a correction recorded in place (meta-rule 1):
  this skill's own earlier text said `gh stack merge <followup-pr>`, and a sibling repo's audit of
  the same text first proposed "inspect with `gh stack view --json` before merging" as the guard —
  a non-mitigation, since `view` takes no target and cannot resolve what a bare number would have
  merged. The independent-numbering hazard is observed fact, not inference: that repo had stack
  #46 live while PR #46 did not exist. The checkout-by-URL cycle replaced both wrong drafts.
- The "a returned command is not a finished merge" rule comes from the API contract: branch
  protection and repository rules are evaluated when the merge *runs*, not when it is submitted, so
  an accepted submit proves only that the PR is open and not a draft. This is the same
  false-green family as the Phase 6 table — an agent reporting success from the submit is reading
  the instrument, not the output.
- The `enqueued`-is-not-`merged` rule exists because it is a *terminal* status for the merge
  request while being a non-terminal state for the actual merge. A poll loop that stops on
  "not pending" and reports success is correct about the request and wrong about the world.
- The exit-0-on-aborted-sync caution comes from the upstream sync contract: on local/remote
  divergence `gh stack sync` prints both chains, changes nothing, and exits 0. Any wrapper that
  branches on exit status alone reads a refusal as a success.
- The `gh stack push` atomicity note is recorded as an unresolved conflict rather than a fact:
  v0.0.8's `--help` claims `--atomic` all-or-nothing while v0.1.0's reference documents per-branch
  leases that can partially apply. Both cannot be true of the same command; until it is worth
  testing, the safe reading is the weaker one.

Append this repo's own batch outcomes here as they accumulate.
