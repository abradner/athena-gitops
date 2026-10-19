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
Workflows target a scale set by its **name**, bare (`runs-on: zagreus-ci`).
A scale set has none of a classic runner's implicit `self-hosted`/`linux`/`X64`
labels; extra labels can be declared (`scaleSetLabels`, chart 0.14+) but only
at creation, so they are not used. The names differ from the legacy runners'
labels on purpose: a job aimed at zagreus can only run on zagreus, which is
what makes "proved by a real job" mean something.

| Scale set | Nodes | Scale | For |
|---|---|---|---|
| `zagreus-ci-light` | arm64 runners (always on) | 1 warm, up to 4 | path filters, doc checks, verify jobs |
| `zagreus-ci` | prefers amd64, falls back to arm64 | 0–6 | general CI |
| `zagreus-ci-x64` | amd64 only | 0–6 | e2e (must not rasterise on arm64) |
| `zagreus-docker-arm64` | arm64 builder (always on) | 0–1 | PR build checks, arm64 release leg |
| `zagreus-docker-amd64` | amd64 | 0–1 | amd64 release legs, registry jobs, transcoder |

Scale-set names are registered per repository, so every repository onboarded
later reuses the same five names under its own `runners/<repo>/`.

**Shared state is a trade, made deliberately.** The caches and the builders'
data roots are shared by every job on a node, PR and main alike, so one job can
leave something for the next: the same single-collaborator trade the
persistent runners make today, recorded in the infrastructure repo's risk
register. Revisit it the day a second collaborator gets push access.

**Warm without being persistent.** Every runner pod lives for one job, but the
expensive state lives in per-node host-path caches under `/var/lib/ci-cache/`
(compiled Ruby via mise, the bun and gem caches, Playwright browsers) and
`/var/lib/arc-docker/` (each builder's dockerd data root and layer cache).
Those survive the pod and a power-off. The runner image's entrypoint hands the
mount points to the runner user (`RUNNER_CACHE_DIRS`). Gems are installed per
pod from the shared download cache, never into the shared Ruby, so concurrent
jobs never write the same gem directory.

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
