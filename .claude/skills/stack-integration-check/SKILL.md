---
name: stack-integration-check
description: Verify that a set of candidate branches actually combines correctly before any of them is pushed or opened as a PR — catching cross-branch semantic forks (two branches implementing the same shared abstraction in opposite directions, both suites green), controls silently deleted by a file-level "merge", and content-without-ancestry branches that make two PRs appear to add the same code. Use whenever more than one branch is in flight against the same package, before opening a stack of PRs, after any agent-performed merge or rebase, or when two reports about the same code contradict each other.
---

# Stack integration check

Per-branch review is structurally blind to what happens *between* branches. Each
branch can be individually correct, individually tested, individually green — and
the combination still wrong. This is the check that runs on the combination.

**Run it as soon as candidate branches exist**, not as a last gate before push. A
semantic fork found while the branches are still local is a one-commit fix; found
after the PRs are open it is an archaeology project across published history.

## What this catches

Every instance below is from the sibling repo this skill was extracted from, and
every one was found by reading git rather than by any suite or report.

| Defect | Why per-branch review misses it | Real instance |
|---|---|---|
| **Semantic fork** | Two branches implement the same shared function in opposite directions. Both suites pass because each only tests cases that behave identically under either rule. | Two branches wrote opposite `scope.Covers` semantics for untargeted-grant-vs-targeted-request — fail-closed on one, fail-open on the other. |
| **Silently deleted control** | An agent "merges" by copying files, taking one package wholesale and dropping another. The merged result is correct, so integration testing passes; the individual PR is not. | A branch deleted `validateCapabilityScopes` from three store chokepoints introduced by the PR below it. |
| **Content without ancestry** | The branch has the right *files* but the branch below is not in its history, so the PR diff re-adds code another PR already adds. | A "merge" that was a file-level copy; `git merge-base --is-ancestor` failed. |
| **Enforcement drift** | The same rule expressed in two places (code and query, boundary and constraint) diverges as one side changes. | Scope filtering in application code vs the SQL `LIKE` queries. |

## Step 1 — Write down the topology

Before diffing anything, write down an explicit **parent map** — each branch's
*immediate* parent by name, `main` only for the bottom one. Every later step keys
off this map, so it has to be branch names, not trunk merge-bases: for a stack
`main → A → B`, `git merge-base main B` returns the trunk ancestor, not `A`, and
anything that treats it as A's tip silently re-includes A's files.

Derive it from the branches themselves, not from what an agent said — and **not from
PR metadata**, because the main entry point (`batch-review` Phase 2) runs *before any
PR exists*, so `gh pr view --json baseRefName` and `gh stack view` have nothing to
report yet. Among the candidates that are ancestors of `X`, the immediate parent is
the *highest* one — the one every other such candidate is itself an ancestor of:

```bash
set -- <branch-A> <branch-B> <branch-C>                    # the candidates, positionally
for x in "$@"; do
  parent=main
  for p in "$@"; do
    [ "$p" = "$x" ] && continue
    git merge-base --is-ancestor "$p" "$x" || continue     # p is below x
    if [ "$parent" = main ] || git merge-base --is-ancestor "$parent" "$p"; then
      parent=$p                                            # p is higher than the best so far
    fi
  done
  echo "$x: parent=$parent"
done
```

That yields `main` for the bottom branch and for genuinely independent candidates, and
the branch immediately below for each rung of a stack. It is order-independent, and it
distinguishes *immediacy* from mere ancestry — which the pairwise check below cannot:
in `main → A → B → C`, "A is an ancestor of C" is true and useless.

Use `set --` and `"$@"` rather than a `CANDIDATES="a b c"` string: zsh does not
word-split unquoted parameters, so the string form runs the loop **once** with the whole
list as a single branch name and reports one bogus `parent=main` — silently, since every
`is-ancestor` on a nonexistent ref just fails. Verified in both shells against a real
`main → A → B → C` stack with an independent fourth branch.

Record the result, then prove every link you are relying on:

```bash
git merge-base --is-ancestor <lower-branch> <upper-branch> && echo ANCESTOR || echo "NOT AN ANCESTOR"
```

`NOT AN ANCESTOR` on a branch that claims to build on another means the merge was
a file-level copy. Fix it with a real `git merge` onto a fresh branch, then
confirm the PR diff no longer contains the lower branch's changes.

## Step 2 — Justify every deletion against the branch below

For each stacked branch:

```bash
git diff <lower-branch>..<upper-branch> -- <shared-paths>
```

Read the `-` lines. **Every deletion needs a reason.** A deleted validation call,
a deleted error return, a deleted `if` — these are the shape the defect takes, and
they are invisible in a diff against `main` because the code being deleted was
never on `main`.

If the branch introduced a named enforcement chokepoint anywhere in the stack,
grep for it on every branch above:

```bash
git grep -n '<enforcement-symbol>' <upper-branch> -- <paths>
```

Absent where it should be present is the finding.

## Step 3 — Find the shared surface and compare implementations directly

List files and symbols touched by more than one branch — **each branch against its own
parent from Step 1, never all of them against `main`.** In a linear stack (which
`batch-review` Phase 3 mandates) every upper branch contains all the lower ones, so
diffing every tip against trunk re-reports inherited files as multiply-touched: branch 1
changes `a`, branch 2 changes only `b`, and `a` still shows up twice. That noise buries
the genuine overlaps this step exists to surface.

```bash
# parent names come from Step 1's map — the branch to its left, not a trunk merge-base
git diff --name-only main..<branch-A>
git diff --name-only <branch-A>..<branch-B>
git diff --name-only <branch-B>..<branch-C>
# ...then: sort the combined output and take the duplicates
```

For genuinely independent candidates (no stack yet), every parent *is* trunk and this
reduces to the one-liner form — `for b in …; do git diff --name-only main..$b; done |
sort | uniq -d`. Under a linear stack that same one-liner is wrong, which is the whole
reason Step 1 records names.

For each shared file, read the actual competing implementations side by side —
`git show <branch-a>:<path>` against `git show <branch-b>:<path>`. Do not compare
test results, and do not ask whether both suites are green: **green suites are
what hide a semantic fork**, because each branch's tests were written to match
that branch's semantics and the divergent cases were simply never written down.

For each divergence, build the truth table by hand — every combination of the
inputs that distinguish the two rules — and check which cells each branch's tests
actually cover. The empty cells are the fork.

## Step 4 — Both artifacts must be correct

There are two artifacts and reviewers judge different ones:

- **The merged result** — what ships, what integration tests exercise.
- **Each branch as its own PR diff** — what a human reviewer reads.

"Merged-result-correct but per-PR-wrong" is a real failure state, not a
technicality. Check both explicitly and say which one any claim refers to.

## Step 5 — Test the combination, not the parts

Merge the candidate stack onto a scratch branch and run the full suite there, from
a clean state — **in its own worktree, never by moving the checkout you are working
in.** This check runs while branches are live and often mid-flow from another skill;
switching the shared checkout to a combined scratch branch leaves the next phase
submitting or mutating the combination instead of the candidate it meant to. The
start point is explicit and remote (`AGENTS.md` §8, Git hygiene in shared checkouts):

```bash
git fetch origin main
git worktree add ../integration-<date> -b scratch/integration-<date> origin/main
cd ../integration-<date>
git merge --no-ff <branch-1> <branch-2> ...   # resolve conflicts by union, reading both sides
```

Then the repo's real validation — the exact commands in `AGENTS.md`'s Agent Guidelines &
Operation section, including any version-manager prefix a fresh shell needs, plus any drift
check the repo defines for generated code. Fresh database/services if the suite uses them.

Expect conflicts in manifests and lockfiles when two branches each add a
dependency. That conflict is a **union**, not a choice — take both, and prove it
builds rather than assuming.

The scratch worktree is disposable — remove it when the verdict is written, **from
outside it**:

```bash
cd <original-checkout>                        # never remove the directory you are standing in
git worktree remove ../integration-<date>
git branch -D scratch/integration-<date>
```

Removing the worktree you are `cd`'d into succeeds and leaves the shell in a deleted
directory; the very next git command then fails with `fatal: Unable to read current
working directory`, which reads like a broken repo rather than a cleanup ordering
mistake. It exists to prove the combination, not to ship, and a left-behind scratch
branch is one more thing a later session can mistake for a candidate.

## Step 6 — Check for enforcement drift

Where one rule is expressed in two languages or two layers, verify they still
agree, and that any claimed separation actually holds. If a code-side check and a
query-side filter are both said to enforce the same property, either prove the two
can never see the same inputs (and write down *why*, in code) or apply the
deletion test (`AGENTS.md` §8, Layered checks): enforcement exists once, and any
second copy must change politeness only, never possibility.

## Read the artifact, not the report

Every defect in the table above was found by reading git, and at least one was
*hidden* by trusting a report. Two agents once contradicted each other about
whether a security control existed; both were correct, about different artifacts,
and only `git diff` between them resolved it — and in resolving it exposed the
real defect neither had named.

So: before relaying any claim that would change what ships, check it yourself, and
tell the operator which you are relaying — what you verified, or what you were
told. On any workflow or agent failure, `git branch` and `git log` before assuming
the work was lost; agents routinely commit correct work and then fail only at
reporting it.

## Output

A short verdict per pair of branches that share a surface: **agrees**, **diverges
(with the specific case)**, or **deletes (with the specific symbol)**. Plus one
line on whether the combination builds and tests green from a clean state. Do
not report per-branch suite results as evidence about the combination — that is
the exact substitution this skill exists to prevent.
