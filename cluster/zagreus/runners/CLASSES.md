# Runner classes: one way to ask for a runner (design, 12 Nov 2026)

**Status: proposed, not built.** Asked for by the operator on 12 Nov 2026, once
the classic LXC runners were stopped: every runner can run any job, light and
heavy stay apart, and each job says which architectures it accepts. The Macs
take work only when zagreus has no free slot. This file is the design; each
step below lands as its own PR, and this file is updated as they land.

## Today

| Job asks for | Who answers |
|---|---|
| bot PRs: a set's name (`zagreus-ci-light`, `zagreus-ci`, `zagreus-ci-x64`) | that set only |
| human runs: `RUNNER_CI*` variables, falling back to class labels (`spritz-ci`, `X64`, `playwright-full`) | `zagreus-ci` (it carries the class labels too), `zagreus-ci-x64`, and the two Mac runners (`ARM64`, `spritz-ci`, `playwright-full`), whichever is free first |

- `zagreus-ci` and `zagreus-ci-x64` run the same pod on the same amd64 nodes.
  `ci-x64.yaml` says it is kept only until spritz stops naming it.
- The classic LXC runners (CTs 137, 138, 139) are stopped (12 Nov 2026).
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
| `zagreus-ci` | `self-hosted, linux, heavy, X64` |
| `zagreus-ci-light` | `self-hosted, linux, light` (it already runs on both architectures) |
| a Mac overflow runner | `self-hosted, linux, heavy, ARM64` |

`spritz-ci`, `spritz-light` and `playwright-full` go. `heavy` implies the full
browser set, which every heavy runner has.

### Jobs

| Job | `runs-on` |
|---|---|
| rspec, rspec_dst, vitest, lint, static_analysis, analytics_contract, chromatic, marketing_e2e | `[self-hosted, heavy]`: any architecture |
| e2e, e2e_moments, e2e_print, e2e_full_matrix | `[self-hosted, heavy, X64]`: the Chromatic archives must never rasterise on arm64 |
| changes, agents_md, e2e_full_matrix_verify | `[self-hosted, light]` |

**Bot PRs keep naming the set** (`zagreus-ci`, `zagreus-ci-light`). That
split stays on purpose: a bot PR runs third-party code (new packages' install
scripts), and a set's name is something only zagreus carries, so a bot job can
never reach a Mac, however its labels read. The `RUNNER_CI*` variables and the
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
  `X64`, never a set name), is **not from a bot PR**, and has waited
  **25 s**, the agent asks GitHub for a just-in-time runner config
  (`POST /repos/abradner/spritz/actions/runners/generate-jitconfig`, labels
  `self-hosted, linux, heavy, ARM64`) and starts the existing runner image
  with it (`ACTIONS_RUNNER_INPUT_JITCONFIG`: the image's ARC mode, already
  built). The runner takes one job and is gone.
- 25 s is under the waker's 60 s grace before it wakes a metal node, so a Mac
  covers a burst instead of a wake. ARC registers within seconds, so zagreus
  always gets first refusal.
- A closed laptop or stopped agent leaves nothing behind: no offline runner,
  no waker change. If ARC and the Mac race for one job, the loser's runner
  gets no work and is removed after a timeout.
- The agent uses the credential the Mac runner uses today (a fine-grained
  token with Administration on the repo, which `generate-jitconfig` needs),
  read once at start and never passed to the runner container.

**The floor limits what a Mac ever sees.** `zagreus-ci` keeps claiming up to
six jobs while its nodes sleep (`../waker/README.md`, "zagreus-ci's floor"),
because a cap raised from 0 can leave GitHub holding the job for half an hour.
Those six wait for a wake. So a Mac takes only what is beyond the floor plus
any awake slots: the seventh job of a burst onward. Lowering the floor would
give the Macs more, but brings back the hold. **This design keeps the floor.**

## Steps (one PR each)

1. **athena-gitops: add the class labels** to `zagreus-ci` and
   `zagreus-ci-light`, keeping the old ones. Nothing routes differently yet.
   Waker tests updated for the new labels.
2. **spritz: switch `runs-on`** to the table above. Bots by set name, humans
   by labels. While `zagreus-ci-x64` exists, an `X64` heavy job may land on
   either set: same pod, same nodes.
3. **athena-gitops: fold `zagreus-ci-x64` into `zagreus-ci`**, drop the old
   labels, remove `slotsShared` and the x64 floor from the waker, move the
   x64 set's node pins and `maxRunners` into `ci.yaml`.
4. **spritz: the Mac overflow agent** (`deploy/ci-runner/`), replacing the
   standing Mac runners, then deregister runners 26 and 27. The spritz
   repository variables `RUNNER_CI*` are deleted after step 2 has run cleanly.

## Proof, per step

- After 2: a human-run CI pass lands every heavy job on `zagreus-ci`, every
  e2e job on an amd64 node, and no job on a Mac. A bot PR's run likewise.
- After 3: the same, with one heavy set; the waker logs no "shared labels
  only" warnings.
- After 4: with the metal nodes asleep and a Mac agent running, a burst of
  more than seven heavy jobs puts the overflow on the Mac within about 30 s,
  wakes no node for it, and never puts an `X64` job or a bot job there. With
  the agent stopped, the same burst wakes metal and nothing is left
  registered.

## Open

- Whether ARC's listener ever holds a job it has acquired at its cap, so the
  Mac's runner could not take it. The waker's "held by GitHub?" warnings say
  something like it happens; step 4's proof is the test.
- The operator may add a Pi 5 as an arm64 heavy node. It would carry
  `heavy, ARM64` in a zagreus set of its own, and the Mac rule above is
  unchanged.
- Sharding the heavy suites into lighter jobs is a separate thread.
