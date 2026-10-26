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
node waker (`waker/README.md`) holds it at 0 while the amd64 nodes sleep and
raises it to the awake nodes' slots when the classic runners are saturated.
It drives `zagreus-ci`, `zagreus-ci-x64` and `zagreus-docker-amd64`; their
Applications ignore `spec.maxRunners`, and the value written in each is the
waker's ceiling. Until the waker leaves observe mode the static caps below
apply as written, and with the amd64 nodes always on, `zagreus-ci` races the
classic runners for up to three jobs at a time rather than waiting for them
to fill. Each job class matches at most one scale set; a job matching two
would go to either, so a heavy job could land in a light pod. The scale-set
name stays a label of its own, so `runs-on: zagreus-ci` still means zagreus
and nothing else (dependency-update PRs use it: zagreus is where third-party
code runs, above).

| Scale set | Labels (besides its name) | Classic runners sharing them | Nodes | Scale | For |
|---|---|---|---|---|---|
| `zagreus-ci-light` | `self-hosted, linux, spritz-light` | the three x64 LXC runners and the two Mac runners, once they carry `spritz-light` (none do yet) | prefers arm runners, may use arm64 builder nodes, falls back to amd64 | 1 warm, up to 2 while on the builder | path filters, doc checks, verify jobs |
| `zagreus-ci` | `self-hosted, linux, spritz-ci, playwright-full` | the three x64 LXC runners and the two Mac runners carry both today | prefers amd64, falls back to arm64 | 0–3 | general CI, the full browser matrix |
| `zagreus-ci-x64` | `self-hosted, linux, X64, spritz-ci` | the x64 LXC runners (e2e asks for `X64, spritz-ci`) | amd64 only | 0–1 | e2e (must not rasterise on arm64) |
| `zagreus-docker-arm64` | none: name only (`docker-arm64.yaml` says why) | none; the Mac builders keep `docker-arm64` | arm64 builder (always on) | 0–1 | bot-authored PR build checks; not release legs while they would share its data root |
| `zagreus-docker-amd64` | `self-hosted, linux, X64, docker-amd64` | none once ci-builder-core retires | amd64 | 0–1 | amd64 release legs, registry jobs, transcoder |

Two rules keep the overflow honest:

- **Labels reach GitHub only when ARC creates the scale set** (chart 0.15.0:
  the controller sends them in `CreateRunnerScaleSet` and never updates them;
  actions-runner-controller#4424). After changing `scaleSetLabels`, delete
  that AutoscalingRunnerSet while it is idle
  (`kubectl -n <arc-runners|arc-builders> delete autoscalingrunnerset <name>`)
  and let Argo recreate it. Note the set's id first
  (`kubectl -n <ns> get autoscalingrunnerset <name> -o jsonpath='{.metadata.annotations.runner-scale-set-id}'`),
  then check the labels landed: the recreated set's id must differ. If it does not, the old
  GitHub-side set survived the delete and was adopted with its old labels.
  Recreate a set only while it is idle and nothing is queued for it by name:
  deleting it fails any job running on it, and jobs that name a set have no
  classic fallback.
  For a set the waker drives, recreate it while the waker's nodes are awake:
  Argo creates it at the ceiling, and until the waker's next pass (30 s) it
  could claim jobs for nodes that are asleep.
  - For either docker set, the old dind pod must be gone before the new
    set can start one, since both would use the node's one dockerd data root.
    Turning off auto-sync on its own Application does not hold, because
    `zagreus-runners` and `zagreus-root` self-heal it back within seconds.
    Turn it off top-down instead (`zagreus-root`, `zagreus-runners`, then the
    set's Application), delete the set, wait for the pod to finish
    terminating, then turn sync back on bottom-up.
  - Recreate `zagreus-ci` only once spritz's light jobs ask for
    `spritz-light`. Until then they ask for `spritz-ci` (RUNNER_CI_LIGHT is
    set to the classic labels), so they would race onto 6 GiB heavy pods and
    use up the set's three slots on every run's critical path.
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

## The node waker

`waker/` (its own Application, `waker.yaml`): powers the amd64 nodes on
through Intel AMT when the classic runners are saturated, off after an idle
hour, and drives the scale sets' `maxRunners` to match. It ships in observe
mode; `waker/README.md` has the design, the go-live steps and the rollback.

## Not yet here

- **A pull-through registry cache**, so each image crosses the uplink once (P2c).
- **An alert for long-Pending runner pods.** ARC has no pod-start timeout, so a
  job whose pod cannot schedule waits silently in GitHub's queue (unassigned
  after 24 h).
