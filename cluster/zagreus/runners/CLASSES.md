# Runner classes: one way to ask for a runner (design, 12 Nov 2026)

**Status: approved 12 Nov 2026. Steps 1 and 2 are live (13 Nov 2026); this file's step 3 is the change that folds the x64 set; step 4 is not built.** Asked for by the operator on 12 Nov 2026, once
the classic LXC runners were stopped: every runner can run any job, light and
heavy stay apart, and each job says which architectures it accepts. The Macs
are to take work only when zagreus has not picked it up (today they take it
first: below). This file is the design; each
step below lands as its own PR, and this file is updated as they land.

## Today

| Job asks for | Who answers |
|---|---|
| bot PRs: a set's name (`zagreus-ci-light`, `zagreus-ci`, `zagreus-ci-x64`) | that set only |
| human runs: `RUNNER_CI*` variables, falling back to class labels (`spritz-ci`, `X64`, `playwright-full`) | `zagreus-ci` (it carries the class labels too), `zagreus-ci-x64` (for `spritz-ci` and `X64`; `playwright-full` from step 1), and the two Mac runners (`ARM64`, `spritz-ci`, `playwright-full`), whichever is free first |

- `zagreus-ci` and `zagreus-ci-x64` run the same pod on the same amd64 nodes.
  `ci.yaml` calls the x64 set "the older x64-only set, kept until spritz stops
  naming it". Each has its own slots on the metal nodes (`../waker/waker.conf`;
  the always-on VM's one slot is shared), so together they run more heavy pods
  than either alone.
- The classic LXC runners are stopped (12 Nov 2026).
- A free Mac takes a human job at once, ahead of zagreus: preferred, not
  overflow. The Macs are laptops and come and go.

## The design

### Labels

Two kinds, and nothing else:

- **class:** `heavy` (Rails, Postgres, Chromium: the 6 GiB pod) or `light`
  (the 768 MiB pod);
- **architecture:** `X64` or `ARM64`, the labels GitHub already gives
  self-hosted runners.

| Set or runner | Labels |
|---|---|
| `zagreus-ci` (and `zagreus-ci-x64` until step 3) | `self-hosted, linux, heavy, X64` |
| `zagreus-ci-light` | `self-hosted, linux, light` (it already runs on both architectures) |
| a Mac overflow runner | `self-hosted, linux, heavy, ARM64` |

`spritz-ci`, `spritz-light` and `playwright-full` go. `heavy` takes over
`playwright-full`'s meaning (every browser engine installed). `zagreus-ci`
and the Mac runners carry `playwright-full` today; `zagreus-ci-x64` runs the
same pod and gains the label in step 1, so every heavy runner has it.

### Jobs

| Job | Human runs | Bot PRs |
|---|---|---|
| rspec, rspec_dst, vitest, lint, static_analysis, analytics_contract, chromatic, marketing_e2e, e2e_full_matrix | `[self-hosted, heavy]`: any architecture | `zagreus-ci` |
| e2e, e2e_moments, e2e_print | `[self-hosted, heavy, X64]`: they capture the Chromatic archives, which must never rasterise on arm64 | `zagreus-ci` |
| changes, agents_md, e2e_full_matrix_verify | `[self-hosted, light]` | `zagreus-ci-light` |

e2e_full_matrix (Firefox and WebKit, on main and by dispatch only) asks for
any architecture, as its `playwright-full` label does today.

**Bot PRs keep naming the set** (`zagreus-ci`, `zagreus-ci-light`). That
split stays on purpose: a bot PR runs third-party code (new packages' install
scripts), and a set's name is something only zagreus carries, so a bot job
never asks for labels a Mac could satisfy. The `RUNNER_CI*` variables and the
classic fallbacks go; each job keeps one expression: set name for bots, labels
for everyone else.

### Sets

- `zagreus-ci-x64` is folded into `zagreus-ci` and `ci-x64.yaml` is deleted.
  One heavy queue.
- The waker loses `slotsShared` (#390) and the x64 set's floor; the always-on
  VM goes back to one `zagreus-ci` slot. Simplytics keeps sharing
  `zagreus-ci`'s slots as now.
- Set names do not change, so GitHub's registrations do not churn.
- Out of scope: the docker builder sets (`RUNNER_DOCKER_*`) and the Simplytics
  sets, which already route by name.

### The Macs: overflow, never first

GitHub gives a queued job to whichever matching runner is free first, so a
Mac that is simply registered with `heavy, ARM64` would be preferred. Instead:

- The Mac runs no standing runner. A small agent polls spritz's queued jobs.
- When a job asks for labels a Mac satisfies (`[self-hosted, heavy]`, never
  `X64`, never a set name), is **not from a bot**, **has not been claimed by
  a zagreus set**, and has waited **25 s**,
  the agent asks GitHub for a just-in-time runner config
  (`POST /repos/abradner/spritz/actions/runners/generate-jitconfig`, with a
  `name`, `runner_group_id` 1 (the default group) and the labels
  `self-hosted, linux, heavy, ARM64`) and starts the existing runner image
  with it (`ACTIONS_RUNNER_INPUT_JITCONFIG`: the image's ARC mode, already
  built). The runner takes one job and is gone.
- **Not from a bot** uses the rule spritz's step 2 implements
  (`script/ci_bot_decision.sh`), and the agent applies the same script.
  **The line is merging.** A person merging a bot's pull request is the
  review; after that the code is main's, and every later run builds on it.
  So it looks for *unmerged* bot code: the run's actor, a pull request's
  author, any commit a pull request brings in, and any commit a ref off
  main carries that main lacks (a branch dispatch, or a pull request into
  another branch). A run on main is judged by its actor alone. One
  assumption is unenforced: no bot pushes to main directly (branch
  protection is not available on spritz's plan). *Corrected 13 Nov 2026:*
  this said a push whose commits include a bot's counts as a bot run. Taken
  to its end, that makes every run a bot's, since main always holds merged
  bot code and every pull request builds on it; spritz#1651's review found
  it.
- **This check gates only the start of a runner.** A JIT runner then takes
  whatever matching job GitHub hands it within its idle window (below), with
  no further check. For those later jobs the first line, bot runs naming a
  set (spritz's heavy jobs name `zagreus-ci` when `changes` says
  `bot`), is the only guard.
- **Not claimed by a zagreus set** matters because of the floor (below): a
  job ARC has acquired still shows as queued on GitHub while its pod waits
  for a wake. Whether a runner outside the set can take such a job at all is
  not known (Open); if it can, the agent needs a way to see the claim before
  step 4 ships.
- 25 s is under the waker's 60 s grace before it wakes a metal node, so a Mac
  covers a burst instead of a wake. It is not "zagreus first" in every case:
  an ARC listener restart (6 to 40 s usually, 125 and 303 s seen on 8 Nov,
  `../waker/README.md`) leaves free zagreus capacity unclaimed, and a job
  that waits through one may go to a Mac. That is overflow in the sense the
  operator asked for: work zagreus has not picked up.
- A closed laptop or stopped agent leaves nothing behind: no offline runner,
  no waker change.
- **A runner that gets no job is stopped by the agent, not left to wait.** A
  JIT runner takes whichever matching job GitHub hands it, not necessarily
  the one it was started for. So the agent never stops a runner that is
  working: every 5 s it reads its own runner (`busy` in the runners API), and
  only while that is `false` does it act. It removes the runner when the job
  it was started for has gone elsewhere (another runner, or cancelled), or
  after 60 s idle in any case. Removing is ordered against a job landing
  between the read and the stop: re-read `busy` immediately before, delete
  the runner through the API first, and stop the container only once the
  delete has succeeded. That relies on GitHub refusing to delete a busy
  runner, which is unverified: step 4 must test it before relying on it. An idle runner can
  still take a brand-new job within that window, ahead of zagreus; that
  residual is accepted.
- The agent uses the credential the Mac runner uses today (a token with
  admin rights on the repository, which `generate-jitconfig` requires), read
  once at start and never passed to the runner container.

**The floor limits what a Mac ever sees.** `zagreus-ci` keeps claiming up to
six jobs while its nodes sleep (`../waker/README.md`, "zagreus-ci's floor"),
because a cap raised from 0 can leave GitHub holding the job for half an hour.
Those six wait for a wake. So a Mac takes only what is beyond the floor plus
any awake slots: the seventh job of a burst onward. Lowering the floor would
give the Macs more, but brings back the hold. **This design keeps the floor.**

## Steps (one PR each)

1. **athena-gitops: add the class labels** to `zagreus-ci`, `zagreus-ci-x64`
   (`heavy`, and `playwright-full` to match `zagreus-ci`) and
   `zagreus-ci-light`, keeping the old ones. No job asks for `heavy` or
   `light` yet. One routing change: `e2e_full_matrix` (which asks for
   `playwright-full`) can now land on `zagreus-ci-x64` as well as
   `zagreus-ci`, the same pod on the same amd64 nodes.
   `../waker/tests/test_classes.py` reads the sets' labels and checks the
   routing table above with the waker's own matcher. **Rollout:** ARC writes
   labels only when it creates a scale set, so after the merge each of the
   three AutoscalingRunnerSets (`zagreus-ci`, `zagreus-ci-x64`,
   `zagreus-ci-light`) is deleted and Argo recreates it. That kills a
   running job, so it is done with the set idle, on the operator's go, and
   checked by reading the labels back from GitHub
   (`gh api repos/abradner/spritz/actions/runners` lists ARC's runners only
   while they run, so the check is a job asking for `heavy` being picked up).
2. **spritz: switch `runs-on`** to the table above. Bot runs by set name,
   everyone else by labels, decided by `changes` (the rule above). Both
   amd64 heavy sets carry `heavy` and `X64`, so either takes any human heavy
   job and no heavy capacity is lost before step 3. **Done:** spritz#1651,
   merged 13 Nov 2026; its own CI and the first main run routed every job
   by class. Light jobs ask for `light` even in a bot's run (`changes`
   computes `bot`, so it cannot read it, and only zagreus carries `light`).
3. **athena-gitops: fold `zagreus-ci-x64` into `zagreus-ci`**, drop the old
   labels, remove `slotsShared` and the x64 floor from the waker, move the
   x64 set's capacity into `zagreus-ci` (its per-node slots in
   `../waker/waker.conf` and its `maxRunners`), and make the always-on VM's
   shared slot one plain `zagreus-ci` slot. As built: `zagreus-ci` is
   `self-hosted, linux, X64, heavy` with `maxRunners` 10 (the per-node slots
   in `../waker/waker.conf`) and floor 8 (the old 6 and 2 in one: a first wave
   of five heavy jobs and three e2e jobs changes no cap); `zagreus-ci-light`
   is `self-hosted, linux, light`. The waker keeps `slotsShared` and pinned
   floors as features: its tests for them, and the scenario tests, run on a
   frozen or pre-fold world (`test_waker.frozen`,
   `test_sharing.pre_fold_config`), so only the live-config checks changed.
   The `SET_LABELS` fixture belongs to that frozen world and keeps its x64
   set, so it is left as it was. **Rollout:** dropping labels needs both
   AutoscalingRunnerSets recreated again, idle, on the operator's go;
   deleting `ci-x64.yaml` prunes its Application and scale set, so the merge
   waits for that set to be idle too.
4. **spritz: the Mac overflow agent** (`deploy/ci-runner/`), replacing the
   standing Mac runners, then deregister the two standing Mac runners. The spritz
   repository variables `RUNNER_CI*` are deleted after step 2 has run cleanly.

## Proof, per step

- After 2: a human-run CI pass lands every heavy job on a zagreus amd64 set,
  and no job on a Mac (until step 4 the Macs carry no `heavy`). A bot PR's run
  likewise.
- After 3: the same, with one heavy set; the waker logs no "shared labels
  only" warnings.
- After 4: with the metal nodes asleep and a Mac agent running, a burst of
  more than seven heavy jobs puts the overflow on the Mac within about 30 s,
  wakes no node for it, and never puts an `X64` job or a bot job there. With
  the agent stopped, the same burst wakes metal and nothing is left
  registered.

## Open

- Whether a job a scale set has acquired (a floor claim waiting for a wake)
  can be taken by a runner outside the set. If GitHub reserves it for the
  set, the Mac never sees floor jobs and nothing more is needed. If not, the
  agent must learn of claims (for instance from the waker) before step 4
  ships. Test this first in step 4.
- The operator may add a Pi 5 as an arm64 heavy node. It would carry
  `heavy, ARM64` in a zagreus set of its own, and the Mac rule above is
  unchanged.
- Sharding the heavy suites into lighter jobs is a separate thread.
