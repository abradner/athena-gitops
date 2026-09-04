# Agent Onboarding: Athena GitOps

## Repository Overview
`athena-gitops` is the foundational cluster management repository for the Athena homelab. It maintains raw Kubernetes bootstrap assets, ArgoCD Application manifests, and core cluster infrastructure configuration. A related, separate repository — `tools-workflow` — provides the automation pipeline that drives several operational workflows against this repo.

## Domain Nomenclature
- **Application**: An individual microservice (e.g., `pmn-ext-gw`), mapping directly to the ArgoCD Application schema.
- **Project**: The overall deployment suite scope (e.g., `pmn`). `Config#project_name` is the canonical identifier, sourced from the `PROJECT_NAME` env var and used for namespace interpolation (`#{project_name}-#{env}`).

## Placement Doctrine — what runs where, and why

The single most load-bearing set of decisions in this estate. Stated as principles; the
concrete inventory (which guest runs on which host, addresses, ids) lives in the **private**
`asn-infra` repo, deliberately not here — see "The public/private split" below.

**1. Prod-facing applications run in-cluster; their state does not.** Every app here is
stateless against an external PostgreSQL and an external S3-compatible object store. This is
not incidental: during a month-long cluster outage in 2026, data loss was zero *because* of
this split, and the refresh/restore tooling stays simple because the database is a plain host
rather than a cluster-managed service.

**2. State lives on the hypervisor tier, never on the storage box, never on the Pi
hypervisors.** The hypervisor cluster has the RAM, NVMe mirrors and its own backup server, and
is a separate failure domain from the cluster. The NAS is the *backup destination* — putting a
source there defeats the purpose. The Pi hypervisors that host the control-plane VMs would
contend for memory with those VMs and couple two failure domains.

**3. Nothing that watches the cluster may depend on the cluster.** Monitoring, alerting and
backups run outside it. A detector living inside its subject fails by going silent, which is
indistinguishable from "everything is fine" — this is exactly how the month-long outage stayed
invisible. Only stateless collectors (metric scraper, log shipper, exporters) remain in-cluster;
they buffer to disk and forward outward.

**4. A cache is a third case, not "state".** Durability, survivability and latency are separate
axes and a cache scores differently on each: rebuildable (no durability need), useless while the
cluster is down (no survivability need), hit on every request (latency matters). All three point
**in-cluster**. The exception that flips it: a Redis used as a *job queue* is not
reconstructible and is a database in disguise.

### Rejected alternatives, with the reasons

Recorded so they are not re-proposed as if new.

- **NFS-backed PersistentVolumes from the NAS.** Requested with the condition "as long as it
  won't take down the cluster if the NAS is away for a few hours". That condition is not
  satisfiable: hard mounts wedge kubelet's volume manager, and soft mounts corrupt a
  time-series database mid-write. Inverted to node-local volumes plus scheduled snapshots
  outward, keeping the NAS out of the runtime path entirely.
- **Longhorn and Ceph.** Declined — both add a distributed-storage control plane (and its
  failure modes, and its own backup obligation) to solve a problem the stateless-app split
  already removes.
- **A tainted in-cluster worker for observability.** Solves capacity but not survivability:
  control-plane death still blinds you. Survivability was the whole point.
- **Moving metrics/logs into PostgreSQL** to reduce the number of things to back up. Argues the
  other way: observability data is derived and deliberately disposable, and putting it in the
  database makes it look like something that must be protected.

## File Layout & Separation of Concerns

### `bootstrap/`
Segmented into `talos/` and `kubernetes/`:

- **`talos/`**: Contains `.template.yaml` files with `{{ dotted.key }}` placeholders (e.g., `{{ certs.os.crt }}`). These are rendered into hydrated YAML configs via the `render-talos` command in `tools-workflow`, which reads a single 1Password Secure Note (identified by `OP_TALOS_ITEM_ID` in `tools-workflow/.env`), flattens its YAML contents into dot-separated keys, and substitutes all placeholders. Generated files are gitignored — they contain full cluster PKI and must never be committed.
- **`kubernetes/`**: Provisioning scripts for core networking, Cilium, ArgoCD, and the Gateway.

See [`bootstrap/README.md`](bootstrap/README.md) for the full order of operations.

### `cluster/` (top level)
Synced non-recursively by the self-managing root app (`cluster/root.yaml`, prune off): the `core`/`pmn` AppProjects (`projects.yaml`) and the two child Applications `cluster-core.yaml` / `cluster-apps.yaml`, which recursively own `cluster/core` and `cluster/apps` with automated prune + self-heal. New Applications must set `project: core` (this repo / pinned chart repos) or `project: pmn` (`pmn-workloads` → `pmn-*` namespaces) — not `default`.

### `cluster/apps/`
Overarching ArgoCD sync targets linking ArgoCD to upstream workload repositories (e.g., `pmn-workloads`). The `pmn` suite is defined by a single `ApplicationSet` (`pmn-appset.yaml`) whose matrix generator expands service × environment lists into per-app `Application` resources — add an environment or service by appending to the relevant list, not by adding per-env manifest files.

### `cluster/core/`
Foundational cluster integrations:
- `onepassword-backend` `ClusterSecretStore` for ExternalSecrets retrieval via 1Password.
- ArgoCD GitHub repository authentication via an `ExternalSecret` (`argocd-github-repo-pmn.yaml`) that maps a GitHub PAT from 1Password into the ArgoCD control plane.
- `cloudflared` — two Cloudflare Tunnel connectors (one per Cloudflare account, since a tunnel belongs to an account and the served zones span two). They exist so that public hostnames stop depending on the origin's static IP address, which a WAN failover changes. Their ingress config is a **single catch-all rule** pointed at a dedicated Gateway listener on port 8443 that sets no hostname, so there is no hostname list to drift out of step with `gateway.yaml`. Routes do **not** attach to that listener automatically — every HTTPRoute here pins itself with `parentRefs.sectionName`, so each public route names `https-tunnel` in a second parentRef and internal routes deliberately do not. **A new public site needs that extra parentRef or it will 404 through the tunnel** while still working via HAProxy. An `asn.casa` 404 rule sits ahead of the catch-all: **nothing in that zone is ever tunnelled**, so the internal-only surfaces described in Snapshot Learning 3 cannot traverse a tunnel even if someone creates a DNS record for one. The rule is the whole zone rather than the `athena` subdomain because DNS cannot distinguish the two — `argocd.athena.asn.casa` and `rabalex.wedding` both resolve to Cloudflare anycast, while `kaff.asn.casa` publishes a private address — and because internal services are named under `asn.casa` by convention, so a new one is denied before anyone thinks about it. Anything there needing an external presence moves to another domain first. Cutover procedure: `docs/runbook-cloudflare-tunnel-cutover.md`; the wider design it belongs to: `docs/design-multi-az-boreas.md`.

## Agent Guidelines & Operation
- **Argo App Instantiation**: The `pmn` ApplicationSet in `cluster/apps/pmn-appset.yaml` generates sync targets per application and environment. (Historically the `GenerateArgocd` orchestrator in `tools-workflow` emitted per-environment manifest files; if that tool still regenerates `pmn-dev*.yaml` files it must be updated to edit the ApplicationSet lists instead, or its output will conflict with the ApplicationSet-owned Applications.)
- **Syntactic Validation Only**: This repository uses syntactical YAML verification. Do not attempt behavioural spec logic. A `.git/hooks/pre-commit` hook uses `yq` (via `mise.toml`) to run `./scripts/validate_yaml.sh`, preventing structurally invalid commits.
- **Do Not Hardcode Secrets**: All sensitive values must be templated and sourced from 1Password. If you encounter plaintext certs or tokens in committed files, flag them immediately.

## 8. Working Rules

The portable core. These rules are pre-filled from hard-won convention across this repo's sibling
projects; adjust only with reason, and record the reason in the change that adjusts it.

### Planning & approval

- Propose an implementation plan for any moderately or highly complex change, and get it reviewed
  and approved before making edits. Don't dive into large builds or refactors on your own read of
  the situation.
- For anything genuinely ambiguous or not yet decided by the operator, prefer the reversible
  option and leave a clear marker rather than picking silently. Two-way doors over one-way doors.
- **Building structure where no convention is stated is a decision, not a default.** When a change
  introduces more than trivial structure (a new page/screen, a new service layer, a new module
  shape) and no existing section of this file states a convention for that surface, do not
  silently invent one — and do not treat "match the surrounding code" as cover: over undesigned
  code that instruction *propagates* the blur. Adopt a pattern, then name it in the PR body as an
  explicit proposal ("no stated
  convention for X; this PR uses Y; codify or correct"). The operator's review then decides a
  standard once, instead of re-litigating shape per PR. (Motivating instance: a sibling repo's
  first UI-heavy PR mixed layout, scaffolding, and domain components with no container/presenter
  separation — the agent had nothing to follow, faithfully extended the existing blur, and the
  standard got stated for the first time as a review comment, the most expensive place to state
  it.)

### Landing changes

- **Every change lands through a pull request** — including one-line copy tweaks. Size is not the
  criterion. (Motivating instance: a few commits went straight to `main` during a sibling repo's
  init. Automated reviewers fire on the ready-for-review edge, so a direct push is a change nothing
  reviewed, and `main`'s history stops meaning "reviewed states".)
- Opening a PR is not merging it. Push permission is granted per session — ask once before the
  first push unless already told.

### Destructive & outward-facing actions

- Destructive actions (dropping data, deleting files you didn't just create, rewriting published
  history) and outward-facing actions (publishing, sending, deploying) need explicit approval,
  every time. Approval for one instance is not approval for the next.
- **Merging is gated on a live go-ahead, and every new session starts assuming the manual gate.**
  A completed review, green CI, a finished followup, and an auto-mode session default are none of
  them authorization. Report readiness and stop.
  - A **conditional, forward-looking** go-ahead is still a go-ahead: "merge this train once #38 is
    resolved" names the batch and states the condition, and covers merging while the operator is
    away. It authorizes *that* batch only.
  - A **session-scoped carve-out** ("auto-merge only trivial mechanical PRs tonight") is valid when
    the operator sets it — at session start, as their own policy choice. Treat it as a one-session
    precedent and ask again next time; anything touching auth, scopes, custody, or an API contract
    stacks and waits regardless.
  - Ambiguous continuations ("continue the stack flow") are **not** authorization. Ask briefly,
    citing this rule so it doesn't read as timidity.
  - **Do not propose loosening this.** The operator's position is that they would like to soften it
    eventually but that harnesses are not reliable enough yet. Offering options is fine when they
    are the one setting session policy; advocating for less gating mid-stream is not.
- Never force-push or rewrite published history without an explicit, current go-ahead.
- **Visibility and content are separate axes.** Before making anything public, do not let a narrow
  confirmation (repo name, a visibility toggle) stand in for consent to the *content* — git
  history, comments, and design docs go out too. Separately flag anything describing a
  still-unfixed vulnerability or written more candidly than the operator likely pictured, and ask
  about that specifically. (A sibling repo was pushed public with candid commit messages
  documenting exact vulnerabilities, one of them still live in the code at push time; it had to be
  flipped back to private immediately.)
- If you encounter a violation of a safety rule already committed (a plaintext secret, a
  destructive migration lying in wait), flag it immediately — finding it is not the same as
  having caused it, and silence helps nobody.

### Tooling version floors

- **A version floor is an interrupt, not a workaround.** When a skill or workflow depends on
  tooling at or above some version and the environment is below it, stop and tell the operator
  what to upgrade. Do not silently take a degraded path, reimplement the missing capability by
  hand, or work around it — the operator can fix an install in seconds, and the workaround is
  what ends up load-bearing and unreviewed.
- Distinguish **fixable** from **unavailable**. A missing or outdated tool is fixable: interrupt.
  A capability the platform genuinely doesn't offer here — wrong host, feature not enabled for
  this repo, a documented limit — is unavailable: take the documented fallback and record why.
- Name the floor and the exact upgrade command when you interrupt. "Your `gh` is too old" costs
  the operator a search; "`gh extension upgrade stack` — `merge` landed in v0.1.0" does not.
- The same applies mid-run: if tooling turns out to be below the floor after work has started,
  stop and say so rather than finishing on the degraded path and reporting success.
- **Adding a dependency can raise the project's floor without asking.** Package managers resolve a
  new dep's own requirements by bumping yours. After any dependency add, read the manifest diff for
  the language/toolchain lines specifically; if a dep forced a bump, pin the *dep* to the newest
  version whose floor matches the repo rather than raising the repo. A toolchain bump touches the
  production image and is a deliberate decision, not a side effect of installing something.
  (Instance: `go get <dep>@latest` rewrote `go.mod`'s Go version and deleted the `toolchain` pin,
  breaking the Docker build against a pinned base image. The `test` job still passed — only `build`
  caught it.)

### Configuration

- Fail fast on boot: never provide fallback defaults when reading *required* configuration.
  Missing required config must raise at startup rather than silently degrade. Defaults are fine
  for genuinely optional tuning knobs. (This isn't hypothetical: a sibling repo baked a stand-in
  value into an image-wide ENV to make a build step pass, which would have silently defeated this
  rule in production. If a build step needs a stand-in, scope it to that step, never image-wide.)
- The same explicitness applies to what the code writes: anything persisted that holds data states
  its permissions explicitly rather than inheriting the process umask. (Database dumps in a
  sibling repo landed world-readable because the dump path never asserted a mode.)

### Review feedback

- **A finding is a claim, not a verdict.** Whether it comes from a bot, a human, or your own
  earlier session: trace or reproduce it before acting on it. Ask whether the flagged path is
  actually reachable. When a finding says code and docs disagree, work out which end is wrong
  before "fixing" either. Declining findings has to actually happen — a round that accepts every
  finding is a warning sign, not a good score.
- **Verify a delegated claim against the artifact before relaying or acting on it.** Read the
  diff, grep the branch, run the command — a subagent's report is a claim like any other. (Two
  agents once filed contradictory security reports and *both were correct about different
  artifacts*; only diffing them resolved it, and doing so exposed a real defect — a branch that
  deleted a control introduced by the PR below it, which the final merge accidentally restored.
  Merged-result-correct but per-PR-wrong is exactly what a reviewer catches and loses trust over.
  Separately, an agent has returned a placeholder summary while having done complete, correct work:
  believing the report would have discarded it.)
- Reject suggestions that violate the rules in this file, and say why. Automated reviewers read
  this file too; that's expected — reviewer-side agents should review fully as normal, and rules
  here that bind only author-side agents say so explicitly.

### Testing & verification

- **Verify the output, not the instrument.** Green means nothing threw, and nothing more. Before
  claiming something works, name the artifact it should have produced and go look at it — the
  rendered page, the written file, the actual rows. A tool reporting on itself is not the
  artifact: `git bundle verify` once passed on backup bundles that could not actually be cloned,
  because the verifier checks internal structure, not that the bundle can reconstitute a repo —
  the suite that replaced it performs a real clone.
- **Prove a new test can fail.** For any bug fix: write the regression test, confirm it passes
  with the fix, revert just the fix, confirm the test fails for the right reason, restore the fix.
  A test that was never seen to fail hasn't proven anything. This is part of writing the fix, not
  review debt to defer — one reviewer's "add regression coverage for your own fix" finding recurred
  three times across a single PR series and was right every time. And the mutated code must
  *compile*: a build-failed mutant proves nothing (re-learned three times in one night).
- **For any isolation or authorization test, name the attacker and write *their* request.** A spec
  that asserts the mechanism's own definition back at itself cannot fail for any input — it reads
  like coverage and gates nothing. If you cannot describe an input that would make the assertion
  fail, the test is documentation, not a gate. (A tenancy spec built its record through the very
  scope it claimed to test; the actual cross-tenant hole sat unnoticed until a reviewer read the
  middleware and the controller together.)
- **Inference from a plausible nearby cause is not diagnosis.** Wait for the real signal and read
  the actual failure before naming a cause. The incident and the review-time form of this rule live
  in `docs/pr-review-machinery.md` §5 — that file is canonical for it; don't restate it here.
- **Watch the setup, not just the assertion.** Four tests in one feature passed against genuinely
  broken code because their setup could never exercise the branch — an assertion on node count
  only, a fixed `now` so the fade never completed either way, a payload rejected as malformed
  before its size mattered. Also beware a test that captures current behavior so faithfully it
  archives the defect.
- Ask what else satisfies your assertion — a count-based check that an empty-state row also
  matches, a visibility check that passes for a scrolled-away element. When asserting absence,
  include a positive control so a broken probe can't read as success.
- Every conditional branch that encodes real logic gets a test that exercises it — especially the
  rare/edge branches, not just the happy path.

### Git hygiene in shared checkouts

- **Create branches only from an explicit start point** (`git checkout -B <name> origin/main`) when
  anything else might be operating in the same checkout, and check `git branch --show-current`
  before any operation that depends on HEAD. (A "one-line docs PR" silently inherited five feature
  commits from an in-repo builder agent's branch and was reviewed in that state. "Only one builder
  running" is not "only one git user"; prefer worktree isolation for delegated builders.)
- **Never pair `git stash` with a later `pop` unless the stash verifiably created an entry.** A
  stash on a clean tree stashes nothing, and the paired pop then pops whatever stranger's entry was
  on top of a stack you don't control. For "test against a clean copy", use `git show REV:path` or
  a scratch worktree instead of stashing at all.

### Layered checks

- Apply the **deletion test** to any validation that exists in more than one place: enforcement
  exists exactly once; anything layered on top must, if deleted, change only politeness (a
  friendly error instead of an ugly one), never possibility. If deleting either check would make a
  new state possible, you have enforcement in two shapes, and they will drift.

### Documentation & discovered work

- Docs describing a boundary or behavior change in the same PR as the change. A doc describing a
  boundary that no longer exists is worse than none, because it is trusted.
- Log discovered work (bugs found in passing, deferred improvements) in the external tracker —
  never a PR body or a code comment. A triage table in a PR body goes stale between rounds and
  vanishes on merge.
- When you fix a subtle bug or get burned by a non-obvious behavior, write the general form of the
  lesson into Gotchas & Lessons Learned before moving on — same session, not later.
- **A review comment that names a missing standard forks.** "We need a convention for X" /
  "this pattern was never established" is not an instance finding — resolving it means both
  fixing the instance *and* codifying the standard into the relevant section of this file (adding
  one if none exists) in the same round; if the standard needs real design time, ticket it in the
  tracker with the review link.
  A convention stated only in a review thread doesn't exist for the next session — the thread
  dies with the PR, and the next agent re-improvises. (This is Gotchas & Lessons Learned's
  ratchet, aimed at conventions: incidents accrete there, review-discovered standards accrete
  into whichever section of this file they belong to.)

### Tooling

- Use the agent's structured file tools rather than `cat`/`sed`/shell here-docs for inspecting
  and modifying files during a session. (Scope: this governs session file operations; committed
  shell scripts do what shell scripts do.)

### Which shipping flow

- The default is one PR at a time: react to feedback immediately, merge when green. Use
  `.claude/skills/single-pr` — it makes that default rigorous rather than merely simple.
- Several PRs open together as one body of work is a different regime: use
  `.claude/skills/batch-review` (fan out, feedback write-only until synthesis, one followup PR).
  It is opt-in for genuine multi-PR fan-outs, not a replacement for the default. The tell is
  reviewer attention fragmenting across live threads, not the size of the diff.
- Both skills read `docs/pr-review-machinery.md` for the parts that don't differ between them —
  reviewer triggers, the three-surface comment harvest, triage, the round cap, and the green-signal
  traps. It is the canonical copy; don't restate it in a skill, and don't let a skill contradict it.
- When more than one branch is in flight against the same code — or any branch went through an
  agent-performed merge or rebase — run `.claude/skills/stack-integration-check` before opening
  PRs. Per-branch review is structurally blind to what happens between branches; this is the check
  that runs on the combination.

### Context & compaction

- When the operator signals they are near the context limit and about to compact, use
  `.claude/skills/park-context` rather than improvising a summary. Compaction keeps a paraphrase
  and discards the transcript, so the park writes only what compaction destroys — intent, rejected
  alternatives, what was actually verified versus assumed, what was mid-flight when the turn was
  cut — and never the diff, which is reconstructible.
- Do not finish work, commit, or push while parking. Parking is triggered by interrupting a live
  turn, so the tree may hold a partial edit nobody intended; record it as observed and stop.
- Resuming from a handoff uses `.claude/skills/resume-context`. The handoff is a claim, not a
  verdict (see Review feedback above) — the session that wrote it is gone and cannot be asked what
  it meant. Verify its state claims against the repo and report drift before building on them.
- Durable lessons never live in a handoff. They land in Gotchas & Lessons Learned, or in the
  external tracker, before the work merges — handoff files are gitignored local state and get
  deleted.

## Recent Snapshot Learnings & Insights
1. **1Password API Rate Limiting**: High workload counts can cause ExternalSecret refresh cycles to exceed 1Password's rate limits. `refreshInterval` has been scaled to `24h` / `6h` cycles to reduce authentication pressure.
2. **Application-Scoped Resources**: Centralising shared resources (e.g., `registry-pull-secret`) into a singular infra app disrupts ArgoCD sync dependencies. Resources are scoped per-application (e.g., `pmn-ext-gw-registry`) to maintain clean ownership topologies.
3. **Cluster-Level Networking**: External hostnames for cluster-level services (such as ArgoCD and Hubble UI) use the `*.athena.asn.casa` domain. These hostnames are typically secured via local HAProxy configurations to remain visible only on the local LAN.

## Argo Self-Management Traps (hard-won, 2026-08)

Six distinct failure modes hit while operating this cluster. Each looks like a
different bug; all are Argo behaving as designed in ways that mislead.

1. **A pinned sync operation survives hard refresh.** Merges appear to do
   nothing for 20+ minutes while Argo retries an old revision. Fix:
   `kubectl patch application <app> -n argocd --type json -p '[{"op":"remove","path":"/operation"}]'`

2. **The Application CRD schema silently prunes unknown fields.** Asserting a
   field this Argo version's schema lacks (e.g. `spec.source.helm.kubeVersion`)
   creates a permanent sync loop: git asserts it, the live object can never
   hold it. Fix by removing the field from git, never by re-syncing harder.

3. **Go zero-values are dropped on serialization.** Asserting
   `directory.recurse: false` explicitly loops forever for the same reason —
   `false` is Go's zero-value for bool and is never serialized back, so live
   never matches git. `true` persists fine; the asymmetry is the tell. Omit
   zero-value fields instead of asserting them.

4. **A completed hook Job never re-runs, even when its manifest changes.**
   `Synced` at the current revision does not mean a sync *operation* ran, and
   hooks fire only on operations. A PostSync Job (e.g. Temporal namespace
   registration) that Completed before a new consumer appeared will look green
   and will not have run. Fix: delete the Job, then force a real operation —
   `kubectl patch application <app> -n argocd --type merge -p '{"operation":{"sync":{"revision":"<rev>"},"initiatedBy":{"username":"manual"}}}'`
   (a hard-refresh annotation alone does not trigger hooks). Expect this for
   every new app added to the shared Temporal cluster.

5. **"one or more synchronization tasks are not valid" ≠ "completed
   unsuccessfully".** "Not valid" means the AppProject denied it (check the
   project's permitted destination namespaces); "unsuccessfully" means the
   apply itself failed. They need opposite responses.

6. **A `Synced` CR can mean nothing was deployed.** Argo only knows the CR
   applied cleanly, not that its operator could act on it. Read the CR's
   `.status.reason` (e.g. a generated Service name over 63 characters kept a
   `VMAlertmanager` "Synced" and dead for hours).

## Gotchas & Lessons Learned

Numbered and accreting — add entries rather than rewriting, so they can be cited from code
comments and PR descriptions. Entry shape: **what happened → the actual root cause → the
general rule.** Argo-specific traps have their own section above.

1. **A Helm `null` override did not delete an inherited key; it shipped the word `null`.**
   Pruning the in-cluster observability stack left the log shipper's chart-default sink behind
   with an empty endpoint, which is fatal at boot — log collection was down cluster-wide. The
   fix attempt asserted `<sink>: null` to remove it. Root cause: **Helm's deep merge can add
   keys but never remove them**, and this chart passes its `customConfig` through to the
   rendered output, so the null serialized literally and stayed a sink. General rule: to
   neutralize an inherited chart value, *complete* it into something valid rather than trying
   to delete it — and never assume a value's absence from your own values file means absence
   from the rendered output.

2. **`render before merge`, not `merge and watch`.** The above cost two merges, two Argo syncs,
   a DaemonSet roll and roughly ten extra minutes of outage to discover something a local
   `helm template` would have shown in one minute. A later commit in the same incident also
   silently deleted a control-plane toleration and a resources block, because the edit replaced
   a text span that reached past its intended end. General rule: for any chart-value change,
   render the chart locally and inspect the *rendered* object — and diff the whole change, not
   just the part you were thinking about. Verifying the previous failure mode is not the same
   as reviewing the diff.

3. **A DHCP-supplied domain became a pod DNS search domain and silently blackholed traffic.**
   Control-plane pods could not reach an out-of-cluster service; the client reported request
   timeouts, no packets ever reached the destination host, and the CNI logged no drops. Root
   cause: the control-plane VMs receive an FQDN by DHCP, from which Talos derives a node search
   domain that kubelet passes to every pod on that node. With the default `ndots:5`, a service
   hostname was tried **with the search suffix first**, that suffixed name matched a public
   wildcard DNS record, and the connection went to a CDN edge that accepts and drops the port.
   Worker nodes had no such domain, so only control-plane pods were affected. General rule: two
   defences, both applied — set `machine.network.disableSearchDomain: true` on nodes whose DHCP
   supplies a domain (see `bootstrap/talos/controlplane.template.yaml`), and write in-cluster
   references to names under a wildcarded zone as absolute FQDNs with a trailing dot. Corollary
   for diagnosis: connectivity probes by IP, and name lookups issued as absolute queries, both
   pass while the real path fails — reproduce the resolver's actual search behaviour from inside
   an affected pod.

## The public/private split

**This repository is public.** Internal addresses, guest inventories, host-to-service mappings
and anything else that describes the shape of the private network belong in the private
`asn-infra` repository, not here. When documenting a lesson whose specifics are topological,
write the transferable rule here and keep the inventory there.
