# `cluster/zagreus/` — the CI build cluster

zagreus runs GitHub Actions jobs for private repositories on hardware of its
own, through actions-runner-controller (ARC) runner scale sets. A separate Argo
CD on zagreus syncs this tree; athena's root never sees it (it syncs only the
top level of `cluster/`). Bootstrap: `bootstrap/zagreus/README.md`.

## Why it is not part of athena

CI executes third-party code: a dependency-update pull request runs its new
packages' install scripts on the runner. On athena that code would share nodes
with production and sit one network policy away from everything production
reaches. zagreus is its own cluster on its own network with internet egress
only, so the router is the wall. Public repositories never use these runners
(a fork's pull request would run code on them); they stay on GitHub-hosted
runners.

## The rule that places everything

**No CI job ever runs on a control-plane node, and everything with secret or
cluster-admin access runs only there.** The control plane holds etcd, Argo
CD's cluster-admin, the External Secrets token and the GitHub App key ARC
registers runners with. Every component that can read those (Argo CD, ESO, the
kubelet CSR approver, the ARC controller and every listener, the Cilium
operator) is pinned there with a nodeSelector as well as tolerating the
control-plane taint; tolerating it alone would let a reschedule move it onto a
worker. Runner pods never tolerate it. CoreDNS also tolerates it and holds no
secrets.

A job that escapes its container lands on a worker. The Node authorizer lets
that worker's kubelet read only the secrets of pods bound to it: the runner
pods' own one-job registrations (JIT configs), nothing else. Inside the
cluster a job reaches only the internet and DNS
(`core/runner-network-policy.yaml`); Argo's own network policies close
repo-server and redis to everything but Argo.

## Runners

One Application per repository and runner class, under `runners/<repo>/`.

**Each scale set shares a class label set with the classic runners**
(`scaleSetLabels`), so a job asking for a class's labels can go to whichever
matching runner takes it. It is a race, not a preference: GitHub offers a scale
set up to `maxRunners` jobs at once, whether or not its pods can be scheduled,
while it hands the rest to idle classic runners (probe, spritz run 39882337983,
23 Oct 2026). A scale set at `maxRunners: 0` is offered nothing, and raising it
on the live set takes effect within seconds (runs 39883352061 and 39883642252).
So "classic first, zagreus for the overflow" comes from `maxRunners`: the
planned waker holds it at 0 while the amd64 nodes sleep and raises it to the
awake nodes' slots when the classic runners are saturated, which will need an
Argo `ignoreDifferences` on `spec.maxRunners`. Until then the static caps below
apply. Each job class matches exactly one scale set; a job matching two
would go to either, so a heavy job could land in a light pod. The scale-set
name stays a label of its own, so `runs-on: zagreus-ci` still means zagreus
and nothing else (dependency-update PRs use it: zagreus is where third-party
code runs, above).

| Scale set | Labels (besides its name) | Classic runners sharing them | Nodes | Scale | For |
|---|---|---|---|---|---|
| `zagreus-ci-light` | `self-hosted, linux, spritz-light` | ci-runner-137/138/139, the Mac runners | always-on arm64: prefers arm runners, may use arm64 builder nodes | 1 warm, up to 2 while on the builder | path filters, doc checks, verify jobs |
| `zagreus-ci` | `self-hosted, linux, spritz-ci, playwright-full` | the same | prefers amd64, falls back to arm64 | 0–3 | general CI, the full browser matrix |
| `zagreus-ci-x64` | `self-hosted, linux, X64, spritz-e2e` | ci-runner-137/138/139 | amd64 only | 0–1 | e2e (must not rasterise on arm64) |
| `zagreus-docker-arm64` | `self-hosted, linux, ARM64, docker-arm64` | ci-builder-fulcrum, ci-builder-mac | arm64 builder (always on) | 0–1 | PR build checks, arm64 release leg |
| `zagreus-docker-amd64` | `self-hosted, linux, X64, docker-amd64` | none once ci-builder-core retires | amd64 | 0–1 | amd64 release legs, registry jobs, transcoder |

Two rules keep the overflow honest:

- **Labels reach GitHub only when ARC creates the scale set** (chart 0.15.0:
  the controller sends them in `CreateRunnerScaleSet` and never updates them;
  actions-runner-controller#4424). After changing `scaleSetLabels`, delete
  that AutoscalingRunnerSet while it is idle
  (`kubectl -n <arc-runners|arc-builders> delete autoscalingrunnerset <name>`)
  and let Argo recreate it. Recreate `zagreus-docker-arm64` only once it has
  built a real image in a real spritz job: its label puts release legs on it.
- **`maxRunners` never exceeds what the nodes can schedule.** A claimed job is
  bound to its scale set, so a claim beyond capacity waits in a Pending pod
  while a classic runner could have taken it (seen in the probe above: the
  claimed job sat queued beside an idle classic runner until cancelled). `zagreus-ci` and
  `zagreus-ci-x64` share the amd64 nodes with the amd64 builder and the light
  pods; their caps (3 and 1) are the four 6 GiB pods that schedule under every
  placement, not the five that fit when nothing else runs (`ci.yaml` has the
  arithmetic).

ARC runners themselves show no labels on GitHub's Runners page or in the
runners API: the labels belong to the scale set (actions-runner-controller#4579).

Scale-set names are registered per repository, so every repository onboarded
later reuses the same five names under its own `runners/<repo>/`.

**Shared state is a trade, made deliberately.** The caches and the builders'
data roots are shared by every job on a node, PR and main alike, so one job can
leave something for the next: the same single-collaborator trade the
persistent runners make today, recorded in the infrastructure repo's risk
register. Revisit it the day a second collaborator gets push access.

**Warm without being persistent.** Every runner pod lives for one job, but the
expensive state lives in per-node host-path caches under `/var/lib/ci-cache/`
(compiled Ruby via mise, the bun cache, Playwright browsers, a pre-installed
gem tree) and `/var/lib/arc-docker/` (each builder's dockerd data root and
layer cache). Those survive the pod and a power-off. The runner image's
entrypoint hands the mount points to the runner user (`RUNNER_CACHE_DIRS`).
Gems are never installed into the shared Ruby or a shared writable cache:
`runners/spritz/bundle-cache.yaml` builds one gem tree per node from main's
`Gemfile.lock`, and each pod copies it read-only into a `BUNDLE_PATH` of its
own, so concurrent jobs never write the same gem directory.

**Priority.** `ci-release` (100) for the release builders, general CI at 0,
`ci-light` (−100) and `ci-dev` (−200) waiting rather than preempting.

**Credentials.** One GitHub App, installed on the selected private
repositories, with repository Administration read/write and Metadata read
(required for repository-scope runners on a personal account). Its key is
synced by `core/github-app.yaml` from the CI-only vault into the scale-set
namespaces, and read only by the controller and listeners.

**Runner version.** ARC disables runner self-update, and GitHub stops queueing
jobs for a runner more than 30 days behind the latest release. An outdated
runner exits with code 7, and ARC then switches that scale set's autoscaling
off. Each runner image must track actions/runner releases.

## Not yet here

- **The node waker** (P2b): powers amd64 nodes on when CI pods sit
  unschedulable, and off after an idle hour. A separate controller, to land in
  `core/` once it exists. Until then the amd64 nodes stay on.
- **A pull-through registry cache**, so each image crosses the uplink once (P2c).
- **An alert for long-Pending runner pods.** ARC has no pod-start timeout, so a
  job whose pod cannot schedule waits silently in GitHub's queue (unassigned
  after 24 h).
