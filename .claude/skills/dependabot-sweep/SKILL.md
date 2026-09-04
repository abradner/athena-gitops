---
name: dependabot-sweep
description: Triage and merge open dependency-update PRs on a GitHub repo — Dependabot and/or Renovate. Check CI/mergeability, inspect risky bumps for breaking changes, fold split peer-dependency pairs together (Dependabot) or trust existing groups (Renovate), diagnose and verify red PRs locally, merge one at a time once the operator has given a go-ahead for the batch, handle lockfile-conflict rebases for either bot. The operator's batch go-ahead covers the merges themselves; every push to a branch you don't own, every PR you close, and every triggered or manual force-push needs its own separate approval on top of it — this skill does not merge, push to a bot's branch, or rewrite history on its own authority. Use when the user asks to clear out / go through / merge dependabot or renovate PRs.
---

# Dependency-update sweep

Args: repo as `owner/repo` (ask if not given or infer from `git remote get-url origin` in cwd).

## Which bot

A repo may run Dependabot, Renovate, or both (commonly: Renovate for version updates, Dependabot kept only for security updates once a migration has happened — check `.github/dependabot.yml` and `renovate.json`/`renovate.json5` for which). List both and see what's actually open before assuming:

`gh pr list --repo <owner/repo> --author app/dependabot --state open --limit 100 --json number --jq 'length'`
`gh pr list --repo <owner/repo> --author app/renovate --state open --limit 100 --json number --jq 'length'`

**`app/renovate` only matches the hosted Renovate GitHub App.** Self-hosted Renovate — running from
a scheduled workflow, or under a PAT — authors its PRs as `github-actions`, as a bot account the org
picked, or as a plain user, and none of those match. If the config files say Renovate is configured
but that query returns `0`, don't conclude there's nothing open; find the real author before
believing the count:

`gh pr list --repo <owner/repo> --state open --limit 100 --json author --jq '.[].author.login' | sort | uniq -c | sort -rn`

**Check for truncation on the TOTAL, before grouping.** `--limit` is a maximum, `gh pr list` gives no truncation indicator, and raising the limit moves the cliff rather than removing it. Crucially, the per-author counts cannot tell you: a capped list of 100 might group as `60` and `40`, and neither equals the limit, so the grouped output looks trustworthy precisely when it isn't. Get the total first and compare *that*:

`gh pr list --repo <owner/repo> --state open --limit 100 --json number --jq 'length'`

If it equals the limit you passed, the list is truncated and the real number is unknown-and-larger — re-run higher until it comes back strictly below, and only then group by author or trust any count derived from it. The same applies to step 1's per-bot listing before you believe you have the whole set: a sweep that silently processes the first 50 of 80 PRs looks exactly like a completed sweep.

Group by the login **alone**. Including the title in the grouping key makes every row unique, so `uniq -c` reports `1` for everything and the dominant author never surfaces — the output looks like data and tells you nothing. (Verified against a live repo: grouping by login gave `3 abradner` / `1 app/renovate`; adding the title gave four rows of `1`.)

Whatever login that turns up is the one to substitute for `app/renovate` in step 1 and everywhere below — **`app/renovate` is a placeholder for the hosted app's login, not a constant.** Everything below applies to both bots; call out where they diverge.

## Steps

**This skill confers no authority to merge, force-push, or act on PRs you don't own.** Being
invoked is not a go-ahead. `AGENTS.md` §8 is explicit that a session default, green CI, and an
ambiguous continuation are none of them authorization — and "clear out the dependency PRs" is
exactly such an ambiguity, since it names a triage job and not necessarily a merge grant.
**Establish the operator's merge authority for this specific batch before merging anything**, and
establish it again in a new session: a grant covers the batch it named and nothing after it. If
you are unsure whether you have one, you don't — report readiness and stop, per §8.

That grant is a floor for merging *your own diagnosis*, not a ceiling that covers every action a
sweep can take. Four classes of action each need their own explicit, current go-ahead, over and
above the batch grant, every time they come up — the steps below say so again inline at the point
they happen, so this list is orientation, not the only place it's said:

- **Pushing a commit to a PR you don't own** — the fold in step 4 (Dependabot) and the cross-manager
  one-off fix in step 4 (Renovate) both write a new commit onto a bot-owned branch. That's an
  outward-facing action under §8 ("destructive actions... and outward-facing actions... need
  explicit approval, every time") and is a different, larger thing than the "ask once per session"
  rule for pushing to *your own* PR branch (§8, "push permission is granted per session").
- **Triggering or performing a force-push** — commenting `@dependabot rebase` (step 8) delegates a
  force-push to the bot exactly as surely as running `git push --force-with-lease` yourself does;
  §8's "never force-push or rewrite published history without an explicit, current go-ahead" binds
  the trigger, not just the act. The manual Renovate rebase later in step 8 is the same rule applied
  to a force-push you run directly.
- **Merging** (step 6) needs the live batch go-ahead from above, confirmed against the actual head
  being merged (see the merge-target note in step 6).
- **Closing a PR you don't own** — the merge-and-close fold in step 4 — needs its own go-ahead
  distinct from the push above it and the merge grant; closing someone else's PR is not covered by
  either.

Separately from authority, one class of PR needs a human regardless of what's authorized: a bump
touching **auth, scopes, custody, or an API contract** stacks and waits for a human however clean
its CI looks. Step 7's workflow-scope block is one shape of that, not the only one.

1. List open PRs for whichever bot(s) are active. Use the **actual author logins** established above — `app/renovate` here is shorthand for whatever the discovery step found, and on a self-hosted install it will be something else entirely:
   **Always pipe through `jq`** — raw JSON blows past the output limit at ~20 PRs — and **pass the same `--limit 100` the truncation check above uses**, so this listing is subject to the same guard rather than reintroducing a silent cap of its own:
   ```bash
   gh pr list --repo <owner/repo> --author app/dependabot --state open --limit 100 \
     --json number,title,mergeable,mergeStateStatus,statusCheckRollup \
     | jq -r '.[] | [.number, .mergeable, .mergeStateStatus, .title] | @tsv'
   ```
   Same for `--author <renovate-login>`. Count the rows: if either listing returns exactly the limit, it was truncated — raise it and re-run before treating the set as complete, per the check above. A fixed `--limit 50` here would silently drop the 51st PR of a large sweep, which is the failure that check exists to catch.

2. For each PR, confirm CI passed and `mergeStateStatus` is CLEAN/`mergeable` is MERGEABLE. Set aside any with failing checks and **triage each red PR by cause before routing it**: a peer-locked pair goes to step 4, and anything else — an application test, a platform incompatibility, a real breaking change — gets diagnosed and reproduced locally (step 5) before you conclude anything about it. Peer-locking is the most common cause of red, not the only one; step 4 handles only that shape, so a red PR sent there by default is a red PR nobody diagnosed. Before treating a failing check as a real breakage, consider it might be a **stale log**: a check run from days earlier can have expired GitHub artifact retention and read as a failure with an unreadable log (`BlobNotFound` / 404 on the log fetch) even though nothing about the bump changed. `gh run rerun <run-id> --repo <owner/repo> --failed`, then re-check — a fresh green run means the original "failure" was retention, not code.

3. Risk triage before merging:
   - Major-version bumps or anything with a substantial changelog: `gh pr view <n> --json body` to read the changelog/release notes, then grep the codebase for renamed/removed symbols and breaking API usage. Most "BREAKING" notes turn out not to apply — confirm exposure rather than assuming either way (e.g. an `actions/checkout` v4→v7 break that only affects `pull_request_target` is a non-event in a repo that never uses it).
   - **Confirm every bump is actually forward**, not a downgrade or a bump to a yanked/broken release. This is a general semver trap, not tied to one package — prerelease-identifier ordering makes it a real one, not hypothetical: see step 4's downgrade-guard note for why lexical comparison misorders prereleases at every digit-length boundary, and what does and doesn't fence it off. **activeadmin** is the package that has burned this most often in practice on Dependabot — check every bump this way, not just that one.
   - **Playwright** (`playwright`, `@playwright/test`): CI green isn't enough — CI caches its own browsers, local dev doesn't. Before merging, run `mise x -- bunx playwright install` (or the repo's equivalent) so local browser binaries match the new version. This writes to a **global cache** — `~/Library/Caches/ms-playwright` on macOS, `~/.cache/ms-playwright` on Linux, `%USERPROFILE%\AppData\Local\ms-playwright` on Windows — not the repo, so expect **no git diff and nothing to commit** — don't go looking for one. Confirm the config still loads with `playwright test --list` at minimum, ideally a full run.
   - Everything else with clean CI and no breaking changes found: safe to merge.

4. **Peer-locked pairs are the most common CI failure — but the fix differs by bot.**

   **Dependabot** opens one PR per package, so packages that must move in lockstep (`react`/`react-dom`, `vue`/`vue-server-renderer`, `@storybook/*` families, `vite`/`@vitejs/plugin-*`) each land alone with an unsatisfiable peer range, and **both PRs go red**. Two red PRs for related packages at the same version is this pattern, not two real breakages. Fix by folding them into one branch:
   - Check out the primary PR's branch in a worktree.
   - `bun add <peer>@<version>` (or the repo's package manager) to bring the partner up. Verify the resulting range style matches the rest of `package.json` — `bun add` pins exact (`19.2.8`) where the file may use carets (`^19.2.8`); fix and re-lock if so.
   - Run the suite locally (step 5).
   - **Get an explicit, current go-ahead before pushing.** You're about to write a commit onto a
     branch Dependabot owns, not one you opened — an outward-facing action under `AGENTS.md` §8,
     separate from and prior to the merge/close go-ahead two bullets down, and not covered by the
     batch triage grant. Commit and push to the primary PR's branch only once you have it. Note this
     costs you that bot's auto-rebase from here on (step 8) — for Renovate especially, see the
     exception there.
   - **Stop before merging or closing anything.** The fold is now two further actions the batch grant doesn't cover: merging the primary (§8's merge gate) and closing the partner, which is a PR you don't own. Get an explicit go-ahead for both, then merge the primary and close the partner with a comment pointing at the one that absorbed it. If the go-ahead doesn't come, leave both open and report the fold as ready — a pushed fold is useful work even unmerged.

   **Renovate** groups packages that must move together by config (`packageRules` with a shared `groupName`) — a single PR touching 5–7 packages is normal and by design, not something to split apart or be suspicious of. Don't try to fold Renovate PRs into each other the way you would Dependabot's; if two Renovate PRs for related packages both went red, that's more likely a genuine breakage than a peer-range split.

   **Cross-manager peer-locks are the sharp edge in Renovate repos**, and they don't look like the pattern above. A CI guard can require two files to agree on a version — e.g. a language/tool-version-manager file (`mise.toml`, `.tool-versions`) and a Dockerfile `ARG` that mirrors it, or an app package and a gem/native counterpart shipping on one release train (activeadmin's gem + npm package is one instance) — while Renovate tracks the two sides as **separate dependencies under different managers with no automatic link**. A solo bump on one side breaks the guard; the failure reads like an unrelated build error (`RUBY_VERSION (4.0.5) != mise.toml ruby (4.0.6)`, or similar), not a peer-dependency conflict, so it's easy to miss that this is the same shape of problem.
   - **One-off fix**: check out that PR's branch, edit the sibling file to match. **Get an explicit,
     current go-ahead before pushing** — the same rule as the Dependabot fold above: this is a
     commit onto a branch Renovate owns, gated as an outward-facing action under `AGENTS.md` §8, not
     covered by the batch triage grant. Push only once you have it. This costs you Renovate's
     auto-rebase on that branch going forward (see step 8) — acceptable for a single PR, painful
     across a whole sweep if several sibling PRs land after it.
   - **Standing fix**: add a `packageRules` entry to `renovate.json`/`renovate.json5` grouping the two dependencies by `groupName`, so future bumps land in one PR. Match on the resolved `packageName`, **not** the `depName` printed in the source file — they can differ, especially for tool-manager entries. Example: a `mise.toml` line `ruby = "4.0.6"` has `depName: "ruby"` but Renovate resolves its actual `packageName` as `"ruby-version"`; `bun = "1.4.0"` resolves to `packageName: "oven-sh/bun"`. Guessing the bare file-key as the match target silently groups nothing. Verify any such rule with a local dry-run against a deliberately stale pin before trusting it — **this dry-run mode is safe to run without any authorization**, it makes no writes to GitHub and creates no branches or PRs, only a local log:
     ```bash
     # temporarily edit the pin down a version in both files, don't commit
     RENOVATE_PLATFORM=local RENOVATE_DRY_RUN=full RENOVATE_ONBOARDING=false \
       RENOVATE_REQUIRE_CONFIG=optional GITHUB_COM_TOKEN="$(gh auth token)" \
       LOG_LEVEL=debug npx --yes --package renovate@latest renovate
     # grep the debug log's "packageFiles with updates" section for depName/packageName,
     # and confirm both sides land under the same branchName — then revert the pins
     ```
     `renovate-config-validator` only checks syntax, not that a rule actually matches anything — the dry-run is the only way to know a group isn't silently inert. Node must satisfy Renovate's `engines` range (`npx --package renovate@latest` will otherwise warn `Unsupported node environment` and refuse to run); use a version manager to get a compliant node on `PATH` if the repo's pinned one is out of range. This command has not been executed as part of writing this skill — there is no Renovate-enabled repo available to run it against — so treat it as unverified and confirm it works before depending on it.
   - **Downgrade guard for any group spanning a prerelease-versioned package** — not specific to cross-manager groups, applies to any `packageRules` entry you write. Renovate compares alphanumeric prerelease identifiers **lexically**, so a shorter identifier sorts above a longer one wherever they first differ at a digit: `beta9` > `beta22`, and equally `beta99` > `beta100`. The defect recurs at **every** digit-length boundary — it is a property of the versioning scheme, not one bad range you can fence off and forget.
     `allowedVersions` fences **one** boundary at a time. `'!/-beta[0-9]$/'` excludes single-digit betas, which covers `beta9`-vs-`beta22` and *nothing else* — `beta99` and `beta100` both pass it. Use it only when you know which boundary the package is actually sitting on, note that boundary in a comment beside the rule, and don't present it as a general guard. If the package will cross further boundaries, the only guard that actually holds is pinning the exact version (no range) so every bump — forward or not — requires a human to accept it explicitly, rather than trying to write a pattern that anticipates every future boundary.
     **`ignoreUnstable` is not that fix, and presenting it as one is the same mistake in a different shape.** By Renovate's own documented behaviour, `ignoreUnstable` (default `true`) only stops it from *starting* to track an unstable line when the current pinned version is stable — it does not apply once the current version is already a prerelease. A package already sitting on a beta is, by that same default behaviour, treated as opted into prereleases: Renovate will not jump to a *different* unstable line unprompted, but it will keep proposing every release within the current one, lexical misorderings included. `ignoreUnstable` does nothing to stop that. Don't reach for it here; pin the exact version instead.
     Verify whichever you pick the same way as the `packageName` mismatch above: a dry-run against a deliberately stale pin should show either no update or the correct forward one — never a downgrade. The dry-run is the check that matters, and (as noted above) it has not been run as part of writing this skill. A regex here is not self-evidently correct either: the single-digit `allowedVersions` form above was shipped in an earlier version of this skill as a general guard and wasn't one, and `ignoreUnstable` was then shipped as the fix for that and wasn't one either — both corrected by this rewrite, so treat any *future* edit to this paragraph with the same suspicion.

5. **Running the suite locally — read the repo's own test doc first.** Look for `AGENTS.md`/`CLAUDE.md` and any `.agents/workflows/*.md`, and use the commands they give verbatim. Do **not** reverse-engineer commands from `.github/workflows/ci.yml`: CI runs inside a provisioned container where bare `bundle`/`npm` already resolve correctly, so the yaml omits the version-manager prefix (`mise x --`, `asdf exec`, `nvm use`) that the same command needs on a dev machine.

   This failure is silent and will waste a lot of time if you don't watch for it. A bare `bundle install` installs gems into whichever runtime wins the `PATH` race, then `rspec` passes green having never touched the pinned version. The mismatch only surfaces later — typically when Playwright boots its webserver under the correct runtime and reports `Could not find <gem> in locally installed gems`. If you see that, **suspect your own earlier bare commands before suspecting a broken toolchain install.** Re-run everything through the prefix.

   Sanity check when unsure which runtime a nested shell actually gets:
   `<prefix> -- sh -c 'which ruby; ruby -v'`

   If the repo's docs hand you a bare command that needs the prefix, that's a doc bug worth fixing in the same PR — offer it.

6. **With the operator's go-ahead in hand** (see the gate above — if you don't have one for this batch, stop here and report readiness instead), merge safe PRs **one at a time**. Issue each `gh pr merge` as its own call — never a `for` loop, never `&&`/`;`-chained. Some agent harnesses refuse looped or chained merge commands outright, but the reason holds regardless of harness: one call per merge keeps the blast radius of a mistake to a single PR, and leaves you a decision point between each one where the head-drift check below can actually fire.
   `gh pr merge <n> --repo <owner/repo> --<strategy> --match-head-commit <approved-sha>`

   **`--match-head-commit` is the gate; everything below is how you get the SHA to put in it.** GitHub refuses the merge unless the PR head still equals that commit, so the check happens *inside* the merge rather than as a separate query beforehand. Without it, a bot pushing between your comparison and the merge call gets merged unapproved — a race no amount of careful checking beforehand can close, because the check and the merge are two round trips. (`gh` documents the flag as *"Commit SHA that the pull request head must match to allow merge"*; confirmed present in 2.98.0.)

   **This flag is a version floor, not a preference.** Check it before you merge anything:
   `gh pr merge --help | grep -q -- --match-head-commit || echo "FLOOR NOT MET"`
   If it is missing, **stop and ask the operator to upgrade `gh`, naming one exact command** — "your `gh` is too old" costs them a search, which is the failure `AGENTS.md` §8 calls out by name. Work out which command applies from where the binary lives, and quote that one:
   ```bash
   case "$(command -v gh)" in
     *mise*|*asdf*)          echo "mise up gh   (or: asdf install github-cli latest)" ;;
     *homebrew*|*linuxbrew*) echo "brew upgrade gh" ;;
     /usr/bin/*)             echo "your OS package manager, e.g. sudo apt update && sudo apt install --only-upgrade gh" ;;
     *)                      echo "installed at $(command -v gh) — see https://github.com/cli/cli#installation" ;;
   esac
   ```
   State the floor as the capability rather than a version number you have not checked: *"`gh` needs `pr merge --match-head-commit`; present in 2.98.0, absent in yours."* Then say that the flag is what makes the merge gate atomic. Do not fall back to comparing and then merging as two calls: that is the racy path this flag exists to close, and `AGENTS.md` §8 *Tooling version floors* is explicit that a floor is an interrupt rather than a workaround, because the degraded path is what ends up load-bearing and unreviewed. An outdated tool is fixable in seconds; a merge of an unapproved head is not.

   **The SHA you pass must be the head the go-ahead was given for.** A go-ahead is given at a moment; any push after it — including your own fold or one-off fix from step 4 — moves the branch past what was authorized.

   **Capture the SHAs before you ask, and name them in the request.** Collect each candidate's head
   first, then quote those values in the message asking for the go-ahead, so the approval you get
   back is an approval *of those commits*:
   `gh pr view <n> --repo <owner/repo> --json headRefOid --jq .headRefOid`

   Immediately before issuing each merge, re-query and compare against **the SHA named in the
   request**. **On any mismatch, stop and ask again, naming the new SHA.** Do not merge and
   reconcile afterwards.

   Capturing *after* approval arrives cannot work: a push landing between your request and the
   operator's reply would be silently recorded as approved, and the later comparison would pass on
   content nobody saw. The other easy miss is your own writes — a step 4 fold, a cross-manager fix,
   or a post-rebase head from step 8 all invalidate the named SHA, and the push you performed
   yourself is the one you are least likely to treat as a change. A check with no stated
   consequence on mismatch is not a gate; this one's consequence is stop and re-ask.

   **Use the repo's configured strategy, don't assume squash.** `--squash`, `--merge`, and `--rebase` are distinct, and each is rejected independently: a repo with squash disabled rejects `--squash` while still accepting whichever of the others it allows. Read what's actually allowed and pick accordingly:
   `gh api repos/<owner/repo> --jq '{squash: .allow_squash_merge, merge: .allow_merge_commit, rebase: .allow_rebase_merge}'`
   If the repo's `AGENTS.md` or contributing docs state a preferred strategy, that wins over inferring one from what's merely enabled.

   After each merge, `mergeable`/`mergeStateStatus` on the *other* open PRs commonly reads `UNKNOWN` for 15–30s while GitHub recomputes — this is true for any merge landing on the base branch, not just the PR's own push. Don't treat a transient `UNKNOWN` as stuck; re-poll before concluding anything, and expect several PRs to flip to `CONFLICTING` as a real (if temporary) consequence of the merge — see step 8.

7. **Workflow-scope block.** PRs touching `.github/workflows/*.yml` (docker/*, actions/* bumps) fail with:
   `refusing to allow an OAuth App to create or update workflow ... without 'workflow' scope`
   Not fixable by retrying and not something you can self-serve. Ask the user to run:
   ```bash
   gh auth refresh -h github.com -s workflow
   ```
   then retry. Batch these and report them together rather than hitting the error once per PR.

8. **Lockfile conflicts — the rebase mechanism differs by bot.** PRs touching the same lockfile will go `CONFLICTING` once an earlier one in the family merges. When `gh pr merge` errors with "Pull Request has merge conflicts":

   - **Dependabot**: commenting `@dependabot rebase` delegates a force-push of that PR's branch to
     the bot — it is a force-push by proxy, and `AGENTS.md` §8 ("never force-push or rewrite
     published history without an explicit, current go-ahead") binds triggering one exactly as it
     binds running one yourself. **Get that go-ahead before posting the comment, not after** — the
     batch triage grant doesn't cover it, the same way it doesn't cover the manual Renovate rebase
     below. Once you have it, comment `@dependabot rebase` on the PR, then poll `gh pr checks <n>`
     until green (wait ~90–150s between checks, don't tight-loop). **A rebase changes the head by
     definition, so the SHA named in your merge go-ahead is now stale — re-enter step 6's head
     check and get a fresh go-ahead naming the new SHA before retrying the merge.** Green CI on a
     rebased branch is not authorization to merge it.
   - **Renovate**: it auto-rebases conflicted branches on its own without any comment or trigger — just poll `mergeable`/`mergeStateStatus` every 30–60s until CLEAN again. **That auto-rebase moved the head without you doing anything, so the SHA named in your merge go-ahead is stale: re-enter step 6's head check and get a fresh go-ahead naming the new SHA before merging.** This path is the easiest place in the whole sweep to merge unapproved content, precisely because nothing you did caused the change. This can take a few minutes and isn't always instant; don't assume it's stuck after one or two checks. **Exception**: any branch you (not Renovate) pushed a commit to directly — e.g. the cross-manager fix in step 4 — is thereafter treated by Renovate as manually-edited and **excluded from its own auto-rebase**. That branch will keep re-conflicting every time a sibling PR merges and touches a shared file, and you have to rebase it yourself each time.

     **A manual rebase here rewrites published history on a branch you don't own, so it needs its own explicit, current go-ahead** (`AGENTS.md` §8) — the batch merge grant does not cover it. Ask, every time; approval for one rebase is not approval for the next.

     **Check out the PR's head into its own worktree first.** `git rebase` and a bare-branch-name
     `git worktree add` both act on whatever the current checkout happens to be, not on a name you
     typed once — run this from a checkout of the default branch and you rebase and force-push
     *that*. Get the PR's own base and head rather than assuming `main`; `baseRefName` is not
     necessarily the repo's default branch, and the two can differ:
     ```bash
     base=$(gh pr view <n> --repo <owner/repo> --json baseRefName --jq .baseRefName)
     head=$(gh pr view <n> --repo <owner/repo> --json headRefName --jq .headRefName)
     git fetch origin "$head" "$base"
     approved=$(git rev-parse "origin/$head")   # the tip you are about to rebase from. This is the
                                 # lease expectation for the push at the end — capture it HERE,
                                 # before any later fetch, or the guarantee is lost.
     git worktree add -B "$head" ../sweep-<n> "origin/$head"
                                 # -B forces the local branch "$head" to match origin/$head every
                                 # time. Plain `git worktree add ../sweep-<n> "$head"` (no local
                                 # branch by that name yet) DWIMs a fresh tracking branch — but only
                                 # the first time. On a second sweep, if `git worktree remove` was
                                 # run earlier (it does not delete the local branch), that same
                                 # command silently checks the stale local branch back out unchanged
                                 # instead of picking up origin's new commits — including any commits
                                 # Renovate's own auto-rebase pushed since. Verified by reproducing
                                 # both the first- and second-sweep cases in a disposable scratch git
                                 # repo while writing this skill; `-B` was confirmed to fix it in the
                                 # same test. Not exercised against a real GitHub-hosted PR.
     cd ../sweep-<n>
     git branch --show-current   # must print "$head"
     git rebase "origin/$base"   # resolve conflicts — for a shared version-pin file this is
                                 # usually "keep both sides' bumps", not a real conflict in intent
     git push --force-with-lease="$head:$approved" origin "$head"
                                 # NAMED lease. Do NOT re-fetch "$head" before this push, and do
                                 # NOT use the argument-free --force-with-lease. That form takes its
                                 # expectation from the remote-tracking ref, so a fetch immediately
                                 # before pushing overwrites the expectation with whatever the remote
                                 # now holds — including a commit someone pushed while you were
                                 # resolving conflicts — and the push then silently destroys it.
                                 # Reproduced: with a re-fetch and a bare lease, a third party's
                                 # commit was force-deleted from the remote ("+ 9d0de0d...af10b16
                                 # feat -> feat (forced update)"), and it was gone. Naming the tip
                                 # you actually rebased from makes the push fail instead, which is
                                 # the whole point of a lease.
     ```
     Then wait for CI. If a Renovate-owned branch stops rebasing after ~10+ minutes of patient polling with no new commit appearing on it, that actually is unusual — worth investigating (compare its last commit's SHA/timestamp across checks to confirm it's genuinely stalled and not just slow) before assuming it needs a manual push.

9. Clean up any worktrees created in steps 4/8. **`cd` back to the primary worktree first** — step 8 left your shell inside `../sweep-<n>`, and while `git worktree remove` on your own current directory succeeds, every git command after it fails with `fatal: Unable to read current working directory` because the shell's cwd no longer exists. Reproduced on git 2.47.3; the removal looks clean and the confirmation you were about to run is what breaks. So: `cd` to the main checkout, then `git worktree remove ../sweep-<n>`, and `git worktree list` to confirm none are left — note step 8's fix means a stale local branch of the same name can also be left behind even after the worktree itself is removed; `git branch -d "$head"` clears it once the PR is settled, if you want the repo tidy. Then report: what merged, what's still open and why (conflicts pending, workflow scope, flagged bumps, red PRs whose diagnosis needs a human, or anything held back for a go-ahead that didn't come). For a Renovate sweep, also flag any cross-manager gap found (step 4) as worth a standing `packageRules` group — even if you patched it manually this time, the next bump on either side will hit the same guard again without one.
