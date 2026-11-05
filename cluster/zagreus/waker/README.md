# The zagreus node waker

A guided tour of the source and its behaviour, with diagrams, is in
`walkthrough.html` beside this file.

One pod on the control plane. It keeps zagreus's amd64 runner nodes asleep
until the classic runners can't keep up, wakes them through Intel AMT, and
puts them back to sleep after an idle hour. It also sets each zagreus scale
set's `maxRunners` to the slots on the nodes that are awake, and shares
the CI slots between spritz and Simplytics by their queues ("Sharing").

- Code: `waker.py`, Python standard library only.
- Configuration: `waker.conf` (JSON).
- Manifests: `waker.yaml`.
- Tests: `tests/`.
- It ships in **observe** mode: it reads everything and logs what it would do,
  and writes nothing. "Go-live" below covers the steps out of observe mode.

## Why `maxRunners`, not only power

GitHub offers a scale set up to `maxRunners` jobs at once, whether or not its
pods can schedule. Probes on spritz showed this on 23 Oct 2026:

| Run | Scale set | Result |
|---|---|---|
| 39882337983 | `maxRunners: 1`, no schedulable node | Claimed a job. The job waited on a Pending pod while an idle classic runner with the same labels sat beside it. |
| 39883352061 | `maxRunners: 0` | Offered nothing. Every job ran on the classic runner. |
| 39883642252 | raised live to `maxRunners: 1` | Listener restarted in about 8 s. Claims resumed. |

So powering a node off is not enough. A set whose nodes sleep must hold
`maxRunners` at 0, or it claims jobs it can't run, and those jobs wait for a
boot instead of going to an idle classic runner. The rule `waker.py` enforces
(`compute_caps`):

> **live `maxRunners` = base + the slots of every node that is Ready,
> schedulable and not asleep, never above the ceiling.**
>
> One exception, a set's `floor` (below): `zagreus-docker-amd64` stays at 1
> while its nodes sleep.

The ceiling is the `maxRunners` written in the set's Argo Application, which
stays the static cap from athena-gitops#223. Each of those Applications
carries an `ignoreDifferences` on `spec.maxRunners` with
`RespectIgnoreDifferences=true`, so Argo never writes the ceiling back over the
waker's value.

ARC 0.15.0 rebuilds the listener whenever the AutoscalingRunnerSet's
`maxRunners` changes. `listenerSpecChanged` compares the desired listener,
which carries `MaxRunners`, against the live one, in
`controllers/actions.github.com/autoscalingrunnerset_controller.go` and
`resourcebuilder.go`. A direct patch therefore behaves like the probe's
`helm upgrade`. Never remove the field: ARC reads a missing `maxRunners` as
unlimited (`resourcebuilder.go`, `math.MaxInt32`).

## Floor

A set may carry `"floor": n` in `waker.conf`: the least `maxRunners` the
waker writes for it, offered while its nodes sleep. Only
`zagreus-docker-amd64` has one (1).

**Why.** A job that queues while its set is at 0 can be held by GitHub
after the waker wakes a node and raises the cap: the raise restarts the
listener (above), and the new session can poll empty for a long time.

| Date | Set | Wake to Ready | Job waited |
|---|---|---|---|
| 28, 29 Oct 2026 | `zagreus-docker-amd64` | about 1 min | 6 to 34 min |
| 30 Oct | `zagreus-docker-amd64` | 47 s | 56 min ("Queue watchdog", below) |
| 3 Nov | `zagreus-docker-amd64` | 47 s | 32 min, release v202611030131; the second job was unstuck by cancel and re-run |

Cap 0 is there so a sleeping set sends its jobs to the classic runners. The
builder's jobs name the set (`runs-on: zagreus-docker-amd64`, through the
spritz variable `RUNNER_DOCKER_AMD64`), and no classic runner carries that
name, so for this set cap 0 sends nothing anywhere: it only sets up the
hold. At its floor the cap never changes across a sleep and a wake, the
listener never restarts, GitHub hands the job over at once, and its pod
waits, Pending, for the node.

**The floor is not room.** The caps the waker counts on for demand, spare
and orphans (`spare_caps`) take the floor back off: a sleeping set's floor
slot is a claim with no node, not a free slot. So:

- A job queued on a sleeping floor set is unmet demand and wakes a node at
  once, as before.
- A claim the floor made with no room awake (`floor_claims`) is a need on
  the same pass, not a stuck claim waiting out `saturationGraceSeconds`.
- The floor's slot never reads as "room offered and not taken", so a job
  waiting on a node that will not wake is not set aside as an orphan.
- The sleep procedure checks for an unbound claim again after the assert
  lead, just before the shutdown: the floor keeps claiming through it.

**Pinned, so it never stands down (4 Nov 2026).** The builder's template
names 01, the one node with its slot (`kubernetes.io/hostname` in
`../runners/spritz/docker-amd64.yaml`; `"pinnedToSlots": true` in
`waker.conf`, and a test checks the two agree). A floor claim made while 01
sleeps waits for 01; it can never land on 02 or 03 and preempt CI there.

Before the pin, the floor stood down whenever a node without the builder's
slot was awake (that is still the rule for a set without `pinnedToSlots`):
the claim would have landed there. Then the cap followed 01's free room, and
with 03 up on 4 Nov it went 1 -> 0 -> 1 about nine times in one burst (73
builder cap changes in 48 hours), each a listener restart. For an unpinned
floor set, `holds_a_floor` also keeps its last slot node awake while a
slotless node it could sleep is up; a pinned set needs neither.

**When 01 is down for good.** With 02 or 03 up and 01 failing to wake (in
back-off), a builder job is still claimed, its pod waits for 01, and an
unbound claim blocks every sleep, so 02 and 03 stay up until 01 returns or
the job is cancelled. The builder's jobs could not run anywhere else anyway;
the queue alerts (15 and 45 minutes) report it.

**Wakes it drives.** An unbound floor claim is unmet demand, so it is not
held to the stuck path's one node per episode. That is bounded today
because only 01 has builder slots, and choose_wakes wakes only nodes with
slots for the set. A floor on a set with slots on several nodes, and a
claim that can never bind, would wake each of them in turn.

**What it costs.** A floor set claims every job it matches, asleep or not.
`zagreus-docker-amd64` also carries class labels (`self-hosted, linux, X64,
docker-amd64`): a job asking for only those would be claimed while the
nodes sleep and wait for a wake (a minute or two) rather than run on an
idle classic runner. No spritz job asks for that today. The waker logs a
warning, once a job, for any queued job a floor set matches by shared
labels alone. Only give a floor to a set whose jobs name it.

**Undo.** Set `floor` to 0 (or remove it) in `waker.conf`. The
ConfigMap's name hash rolls the waker pod on the Argo sync; the cap returns
to 0 at the next sleep.

## Caps from slots (built, not enabled)

`"roomCaps": false` would make a set's cap `base` plus the configured slots
of the awake nodes (cordon and untolerated taints still count), instead of
following each node's free room. Room moves with every pod that comes and
goes, and each cap change restarts the set's listener: on 4 Nov zagreus-ci
went 7 -> 6 -> 7 in a burst, and the waker logged jobs "held by GitHub?"
beside free slots on about 70 passes in two days.

It stays off (`"roomCaps": true`) because the slots are per node only on
paper. CI pods may land on any amd64 node, so the scheduler can put four
heavy pods on 01; the builder, pinned there at `ci-release` priority, then
preempts a running CI job. Room counting is what keeps the builder's room
free today. Turning this on needs the per-node split enforced first (a
non-preempting priority for the builder, or a spread limit on CI pods).

## Choice: a bespoke controller, not cluster-autoscaler

The 19 Oct plan asked for cluster-autoscaler's `externalgrpc` provider to be
evaluated first. It doesn't fit, for three reasons:

1. **It reacts to Pending pods, and with `maxRunners` at 0 there are none.**
   A sleeping set claims nothing, so it creates no pod, so the autoscaler sees
   no demand and never wakes a node. The demand that matters is in GitHub's
   queue, which the autoscaler can't see.
2. **The inverse doesn't work either.** Holding `maxRunners` at full capacity
   would give the autoscaler its Pending pods. But that brings back the
   failure the probes measured: jobs bound to sleeping capacity for a boot's
   length while classic runners sit idle. That breaks "classic first".
3. **It can't own `maxRunners`, and it costs more.** The autoscaler drives
   node counts, not a field on someone else's resource, so a second controller
   would still be needed for the caps. `externalgrpc` also needs a gRPC
   server that we would write anyway (power on, power off, node templates for
   scale-from-zero). The autoscaler would be a second process on a 4 GB
   control plane, for nodes that are never created or destroyed, only powered.

Rejected for the saturation signal: the **ARC listener metrics**. The
listener exports assigned, running and idle counts for jobs already offered
to its own set. It does not export `TotalAvailableJobs`
(`cmd/ghalistener/metrics/metrics.go`, `RecordStatistics`). With
`maxRunners` 0 it is offered nothing, so it has nothing to report. Its
metrics describe a set that is already awake.

## What it watches

**Demand** comes from GitHub's jobs API. Each driven set names its queue
in `waker.conf` and the credential it is read with:

| Set | Queue | Credential |
|---|---|---|
| `zagreus-docker-amd64`, `zagreus-ci`, `zagreus-ci-x64` | `"repo": "abradner/spritz"` | `GITHUB_TOKEN` (the default) |
| `zagreus-simplytics-ci` | `"org": "Simplytics"`: every private repository the App's installation lists, archived ones left out (public repositories never use zagreus) | `"github": "simplytics"`, the `simplytics-zagreus-arc` App's installation token |

Each queue is read once a pass. The waker reads every queued job in its
queued and in-progress runs from the last 72 hours. A job is demand for a
driven set when it is in the set's queue and the set carries every label
the job asks for:

- If the job names the set (`runs-on: zagreus-ci`), it counts at once. No
  classic runner carries a set's name, so only zagreus can run it.
- If the job asks for shared class labels, it counts once it has been queued
  for `saturationGraceSeconds` (60). An idle classic runner with those labels
  takes a job within seconds, so a job still waiting after a minute has no
  classic runner free.

That age test stands in for "no idle classic runner matches", so the waker
needs no runners API and no Administration permission. spritz's token is a
fine-grained one with **Actions: read on spritz only**. Simplytics' queue is
read with an installation token of the org's own ARC App (Actions: read,
Metadata: read; "Credentials").

**A queue it cannot read** leaves its sets blind for the pass: they count
no demand and wake nothing, the others still do, and **nothing is put to
sleep** while any queue is unread (a job in it might need the node). Its
queue gauges are left out (absent, not 0); the others stay live. After
`demandBlindSeconds` the waker stops vouching for sleeping nodes, as for a
GitHub outage, so their heartbeat alerts fire. One unreadable repository
makes the whole org's queue unreadable. With every queue unreadable the
pass holds, as before.

**Unmet demand** is demand the set's current caps can't absorb:

```
unmet = demand − claimed − spare
  claimed = min(EphemeralRunners not yet running a job, demand)
  spare   = maxRunners − EphemeralRunners
```

Without `claimed`, a job the set has already claimed would count again: GitHub
still reports it as queued until the runner picks it up. Spare capacity is
filled by GitHub within seconds, so it needs no node.

**Idle**: a node is busy while it hosts any pod that is not a DaemonSet's, a
static pod, finished, or in `kube-system`. Runner pods of any set count,
idle or running: the light set's warm runner can be handed a job at any
moment, and the shutdown's drain would kill that job. So a warm light runner
that fell back to an amd64 node holds it until it takes a job, and its
replacement goes to the arm nodes it prefers. `kube-system` pods don't count,
because the drain moves them. On 23 Oct both coredns replicas sat on
zag-amd64-01. `../core/coredns-pdb.yaml` makes the drain move one at a time.
A node idle for `idleMinutes` (60) is a candidate for sleep.

**Caps go down at once and up at once.** There is one exception, the
first pass after a start. If that pass can't read a node's Lease and has
no earlier reading to fall back on, it waits one pass before lowering, so
a single failed read doesn't make the listeners flap.

**If a ceiling can't be read,** the waker uses the last one it read. If
it has never read one, it only lowers caps, never raises them, and won't
put a node to sleep. Under-offering only sends jobs to the classic runners;
over-offering strands them.

**One pod with a request the waker can't parse,** or a node with no
allocatable, makes that node offer no new slots. The pass carries on
rather than stopping the controller.

**In observe and caps modes, the asleep list is ignored.** Only full mode
maintains it, so a list left over from a step back must not hold a Ready
node out of the caps.

**Alive** means Ready *and* a kubelet Lease renewed in the last 25 s. The
renewal time is taken from the API server's stamp on the kubelet's update
(`metadata.managedFields[].time`), not `spec.renewTime`. renewTime comes
from the worker's clock, which can be skewed on a box just woken. The stamp
comes from the control plane's clock, which is the clock the waker runs on,
so it is right from the first read, even after a waker restart. Keep the
waker on the node that runs the API server, or the comparison is across
clocks again. A failed Lease read reuses the last good answer for up to
60 s, then counts the node down.

**Without AMT, "off" is weaker:** it means the kubelet's Lease has stopped,
plus a 30 s margin, because Talos stops the kubelet before it powers off. A
kubelet that crashes during a sleep, or a shutdown that hangs after the
kubelet stops, is taken as off. Every listed node has AMT, and AMT's power
state is the confirmation whenever it answers. The
kubelet renews it every 10 s, and the Lease stops the moment the node loses
power. The Ready condition takes ~40 s to notice, and never notices if
kube-controller-manager is down. AMT can't be the check either: it dies
with mains power. No fresh Lease, no slots.

## Wake

The trigger is unmet demand for a set, with a sleeping node that has a slot
for it. The waker wakes every sleeping node it needs to cover the demand, in
`waker.conf` order, all at once, and raises the caps once when they are up;
then a 180 s cooldown. One raise matters: every `maxRunners` change restarts
the set's listener, and a job the set turned down for want of room is held
about ten minutes before GitHub offers it again. Waking one node at a time
(0 to 1, then 1 to 3) left a second job queued for 14 minutes beside two
idle slots (26 Oct 2026). A job still queued after `heldAfterSeconds`
(180 s) while its set has spare is taken to be held: for the next
`heldForSeconds` (900 s) it keeps the nodes that can run it awake, but wakes
nothing, since a raise would not bring it back sooner. Past that the held rule lets go. A job still queued past both
windows while its set had spare was offered room and never took it: the
waker sets it aside by id for `orphanForSeconds` (6 h) or until it
restarts, a free slot's worth of jobs a pass. A slot that stays free for
15 min beside waiting jobs means GitHub is not dispatching them to that set,
and more room would not help. After the 6 h the jobs count again, so a
backlog set aside while dispatch was broken comes back and can wake a node.
A true orphan costs one wake and about an idle hour every 6 h, instead of a
wake every idle cycle. Nodes are counted by their room on paper
(`compute_caps`), so sets sharing a node share its memory when the waker
works out how many nodes to wake. The steps, for each node:

1. AMT `RequestPowerStateChange(2)` over WS-MAN (HTTP digest, port 16992).
   Wake-on-LAN is the fallback when it is configured (see "Wake-on-LAN").
2. Wait for AMT to report on, then for the Node to be Ready (up to 10 min).
3. Wait for that node's heartbeat pod to be Ready, then take the node off the
   asleep list. Its heartbeats are flowing by then, and the old assertion
   stays in the alert's 150 s lookback while the first ones arrive.
4. Uncordon the node. Once every node in the wake is up (or has failed), raise
   the caps once.

If any step fails for a node, the others carry on, and that node comes off the asleep list. Its missing heartbeat
then raises `ZagreusNodeKmsgGone`: a node that won't wake is a fault for a
person to look at. The node is held in a 30 min backoff and annotated
`zagreus.asn.casa/waker-state=wake-failed`.

## Sleep

The trigger is the last node in order that has been idle for 60 min, while no
set it serves has unmet demand. The steps:

1. **Lower the caps** as if the node were gone, then wait 15 s for the
   listeners to restart.
2. **Cordon** the node and wait 30 s. If any pod has landed, uncordon, restore
   the caps and stop.
3. **Take the node off the expected-node list.** Add it to the asleep list
   (below) and wait 180 s, so the line has reached the log store before the
   node's own heartbeats stop. Check again for pods.
4. **`talosctl shutdown`** as an `os:operator`, straight to the node. Talos
   cordons and drains it, and the kubelet shuts down gracefully.
5. **Confirm the power state through AMT** within 5 min. If the node is still
   on, AMT powers it off hard. If that can't be confirmed either, the node
   goes back on the expected list (so the alert judges it) and into backoff.

### The asleep list, and the kernel-log alert

`ZagreusNodeKmsgGone` fires for any expected zagreus node whose kernel-log
heartbeat stops (asn-infra `observability/logs/README.md`). Until now it
expected all four nodes. A sleeping node must leave that list *before* its
power goes. That precondition comes from asn-infra.

zagreus has one road to the log store: Talos's kernel-log sender. So the
waker uses it:

- The waker writes the ConfigMap `log-heartbeat/waker-asleep` with three
  keys: `asleep` (node names), `emitter` (the node the waker runs on) and
  `renewed` (epoch seconds). It renews `renewed` every pass while any node is
  asleep, and every 30 s inside a wake or sleep, which can take ten minutes.
- The heartbeat DaemonSet (`../core/kmsg-heartbeat.yaml`) mounts that
  ConfigMap. Only on the emitter node, and only while `renewed` is under
  5 minutes old, it writes `log-pipeline asleep <node>` into the kernel log
  each minute, for each listed name that is a valid DNS label.
- asn-infra records those lines from `cp1` only (`talos:waker_asleep_by_node`).
  The log receiver sets `node` from the connection, so no other node can
  claim another is asleep. Both zagreus heartbeat alerts leave out a node
  asserted asleep in the last 150 s.

What each failure looks like:

- **The waker dies:** renewals stop, the lines stop after 5 minutes, and every
  sleeping node alerts. A dead waker can't silently leave nodes unmonitored.
- **The control plane dies:** its own heartbeat alerts, and so do the
  sleeping nodes.
- **A node is woken by hand:** the waker sees it Ready and takes it off the
  list (`reconcile_ready_nodes`).

## Sharing

Alex, 5 Nov 2026: Simplytics may use zagreus "as long as it's shared
without manual intervention to constantly rejuggle slots". So
`zagreus-simplytics-ci` has no slots of its own. It carries
`"sharesSlotsOf": "zagreus-ci"`, and draws on `zagreus-ci`'s slots on each
node: the two sets together are never offered more than those slots, or
than the node's room. `zagreus-ci-x64` and the builder keep their own
slots and are untouched.

**The rule** (`compute_caps`, each pass):

> Each free slot in the pool goes to the set with queued jobs not yet
> claimed that holds the fewest slots now; a tie goes to the set whose
> oldest queued job has waited longest. Slots no queue asks for go to the
> sets in `waker.conf` order, spritz first.

- **Neither starves.** A set with jobs waiting and nothing held is served
  before a set already holding slots, however long the other's backlog.
- **A busy set is held to its fair share while the other waits.** ARC
  hands a slot freed by a finished job to the same set's next queued job
  within seconds, long before the waker's next pass, so "the next free
  slot" alone would never reach the waiting set. When a set's queued jobs
  find no free slot and it holds less than its fair share, the other
  set's cap drops to *its* fair share, below the runners it has if need
  be. The fair share splits the slots now held one at a time to whichever
  set holds fewest and still wants one (up to its ceiling), ties to the
  oldest queue. The busy set then claims nothing new; its next finished
  job's slot is free at the next pass and goes to the waiting set.
- **Running jobs are never stopped.** ARC never stops a busy runner because
  its set's cap went down; a capped set only stops claiming.
- **A moving slot is lowered one pass and raised the next.** The set
  giving it up is patched first, and the set getting it waits for the
  next pass (30 s): the lowered set's listener keeps offering its old cap
  until ARC has restarted it (about 8 s), and may claim a job meanwhile,
  which the next pass counts (Codex on #345). So the two are never
  offered the slot at once. A lower deferred by the first-pass rule
  ("Caps go down") holds the pool's raises back the same way.
- **With Simplytics idle, spritz is exactly as before.** The leftover goes
  in `waker.conf` order, so `zagreus-ci` gets the whole pool, as it did
  alone (tests: `Shared.test_simplytics_idle_drives_spritz_exactly_as_alone`
  replays a wake and sleep with and without Simplytics configured and
  compares every write).
- **Ceilings bound the share, not the pool.** `zagreus-simplytics-ci`'s
  ceiling (3, its Application) is below the pool's 7; past it, the waker
  gives the free slots to `zagreus-ci`, and a Simplytics backlog past its
  ceiling wakes no node. A pool set whose ceiling was never read is held
  at its live cap (`bound_caps`' rule), so its share is not reserved for
  nothing.
- **A set whose queue is unread gives its share back.** It asks for
  nothing and keeps no recent slots until its queue reads again; its
  running runners keep theirs.
- **A recently used slot is held for `shareHoldSeconds` (600).** A run's
  jobs come in stages, and each slot handed back and forth changes both
  sets' caps, which restarts both listeners; a job turned down meanwhile
  can be held by GitHub for about ten minutes ("Floor", "Queue
  watchdog"). So a set keeps, for ten minutes, as many slots as it used
  lately, but only while no queued job of the other set wants them.

**What it costs.** Every slot that moves between the sets is a cap change
on both, so a listener restart on both, and it reaches the set getting it
a pass after it leaves the other. With the hold, that is about twice
per burst of Simplytics jobs, not twice per job. A spritz job queued just
as a slot moves can wait for GitHub to offer it again, as at a wake.

**Edges where a pool set differs from a set alone**, found by comparing
20,000 random cases of the old and new `compute_caps`, and judged rare
enough to keep. They apply only while a pool is configured. With
Simplytics idle and production's numbers (`zagreus-ci`'s ceiling equal to
its slots, `roomCaps` true) the caps were identical.
- A pool set's ceiling is applied inside the allocation, so room it cannot
  use goes to the next set (`zagreus-ci-x64`) instead of being clamped away.
- In slot mode, a pool set's runners count towards its cap, as in room mode.

**Undo.** Remove the set (and `githubApps`) from `waker.conf`; the pool is
then `zagreus-ci`'s alone again. Its scale set keeps whatever cap the
waker last wrote (see "Rollback").

## Slots

`waker.conf` gives each node its slots per set. Wake order follows the list.
`zagreus-simplytics-ci` has none of its own: it shares `zagreus-ci`'s
("Sharing"):

| Node | Memory (allocatable) | `zagreus-ci` | `zagreus-ci-x64` | `zagreus-docker-amd64` |
|---|---|---|---|---|
| zag-amd64-01 | 30.3 GiB, 7.95 CPU | 2 | 1 | 1 |
| zag-amd64-02 | 30.2 GiB, 7.95 CPU | 3 | 1 | 0 |
| zag-amd64-03 | 14.4 GiB, 3.95 CPU | 2 | 0 | 0 |
| **all three** | | **7** | **2** | **1** (= the ceilings) |

Since 4 Nov 2026 `zagreus-ci` carries `X64` too (every node it runs on is
amd64), so its 7 slots take the x64 class jobs (e2e, e2e_moments) as well as
the general ones, and GitHub hands each job to whichever matching runner is
free. `zagreus-ci-x64` stays, with its 2 slots, while spritz still names it
for bot PRs.

A node's slots must fit on it even when everything else that can land there
does. That means two light pods (768 MiB, 250m each), the bundle fill,
coredns and the DaemonSets: about 2.25 GiB and 0.76 CPU. A heavy pod requests
6 GiB and 1.5 CPU. The builder requests 3.75 GiB and 1.25 CPU with its dind.
Figures from 1 Nov 2026, after the RAM shuffle (01 and 02 at 32 GB, 03 at 16).

- zag-amd64-01 carries three heavy pods and the builder: 18 + 3.75 + 2.25 =
  24 GiB of 30.3, and 4.5 + 1.25 + 0.76 = 6.51 CPU of 7.95. A fourth heavy
  pod would need 8.01 CPU, so CPU binds, not memory.
- zag-amd64-02 carries four heavy pods: 24 + 2.25 = 26.25 GiB, 6.76 CPU. A
  fifth would need 8.26 CPU. The builder's slot belongs to 01, so it never
  lands on 02 or 03 (its template names 01: "Floor").
- zag-amd64-03 (i5-7500, 4 threads) carries two: 12 + 2.25 = 14.25 GiB of
  14.4, and 3.76 CPU of 3.95. It is last in the list, so it wakes last and
  sleeps first.

The configured slots are an upper bound, not a promise. Each pass, the
waker also checks how much room each awake node has left, and offers a set
only what still fits. It counts:

- **Room:** the node's allocatable, less the requests of every pod already
  there (Codex on #238). That catches another set's runner taking the room.
  For example (24 Oct, when 02 had 16 GB), with zag-amd64-01 gone and an
  e2e job on 02, `zagreus-ci` was offered one slot there, not two.
- **Requests:** each set's pod requests come from its AutoscalingRunnerSet
  template, with native sidecars counted in full (the builder's dind is
  one: 3.75 GiB).
- **Order:** sets fill in `waker.conf` order. The builder is listed first,
  because it preempts CI pods. A shared pool is filled by the sharing rule
  at its first set's place ("Sharing").
- **Own runners:** a set's runners already on an awake node count towards
  its cap.
- **Claimed but unbound runners:** a runner the set has already claimed
  but whose pod isn't on a node yet holds room too (Codex on #238). Before
  anything is offered, it is placed on paper on an awake node it fits.
- **Taints:** a node with a NoSchedule or NoExecute taint the runners
  don't tolerate counts nothing. That covers disk pressure, memory
  pressure and a cordon (Codex on #238).

On 24 Oct, with two CI jobs running and two nodes, this gave 3/1/1, the
same as the static table then.

`tests/test_waker.py` (`test_configured_slots_fit_the_nodes`) checks these
sums. Re-check whenever a request, a DaemonSet or a node changes. A node is
listed only if it should be woken: the arm builder is always on and is not
listed (`base` covers any always-on slots).

## Modes

| `mode` | Reads | Patches `maxRunners` | Cordons, publishes, powers |
|---|---|---|---|
| `observe` (shipped) | yes | no (logs "would") | no |
| `caps` | yes | yes | no |
| `full` | yes | yes | yes |

To change mode, edit `waker.conf` and merge. The ConfigMap's name hash rolls
the pod.

## Restarts

The waker is single-threaded and does one wake or sleep at a time. It keeps
its state in the cluster, not in memory:

- the asleep list (ConfigMap);
- the cordon annotation `zagreus.asn.casa/waker-cordon`;
- the node's Ready condition.

After a restart, any Ready node that is listed asleep or under the waker's
cordon is taken back as awake. A restart halfway through a sleep therefore
leaves the node awake, and its idle clock starts again.

## Permissions

- **Nodes:** get and patch, by name: only the nodes in `waker.conf`. Labels
  and taints keep every credential on the control plane, so a waker that
  could patch any node could move them. Add a node to the ClusterRole in
  the same change that adds it to `waker.conf`.
- **Pods:** list, cluster-wide.
- **The four driven AutoscalingRunnerSets:** get and patch, by name, plus
  listing their EphemeralRunners.
- **The four Applications:** get, by name, to read the ceilings.
- **Its two ConfigMaps:** get, patch and create.

## Credentials (ESO, control plane only)

The 1Password item `zagreus-waker` sits in the CI-only vault. `waker.yaml`
lists its fields: the GitHub token (Actions: read, spritz), the AMT login, a
`node=address` map for AMT, and an `os:operator` talosconfig.

- The Secret is mounted only into this pod, which is pinned to the control
  plane. A worker's kubelet can't read it.
- AMT addresses stay in the vault, not in this repo.
- `os:operator` can reboot and shut down any zagreus node, and nothing
  outside zagreus: its certificate comes from zagreus's own Talos CA.

Simplytics' queue is read with the `simplytics-zagreus-arc` App, the one
its runners register with (1Password item `simplytics-arc-github-app`,
copied by its own ExternalSecret into the waker's namespace as
`arc-github-app-simplytics`, mounted at `/github-apps/simplytics`). The
waker signs the App's JWT itself (RS256 in the standard library, no
third-party module) and exchanges it for a one-hour installation token,
renewed five minutes before expiry. The App can also manage the org's
self-hosted runners; the waker only lists repositories, runs and jobs.

## Footprint on the control plane

cp1 is a 4 GB CM5. Measured on 23 Oct 2026 from the kubelet's summary API: a
2.7 GiB working set, 1.2 GiB available, and requests of 1678 Mi of 3.1 GiB
allocatable. kube-apiserver alone is 882 MiB.

The waker was measured in its pinned image (arm64). The test ran the real
`tick()` against the cluster's own nodes and pods (36 pods) plus a synthetic
GitHub queue: 8 runs × 70 jobs in each list, so 1,120 queued jobs counted per
pass, against a real burst of about 64.

| What | Measured | Set |
|---|---|---|
| Waker, steady, 40 passes | 19-20 MiB cgroup, 26 MiB peak RSS | request 48 Mi, limit 160 Mi |
| Same, with 10× the pods (360) | about 40 MiB cgroup | |
| Waker plus `talosctl shutdown`, once per sleep, for seconds | talosctl 70 MiB peak RSS, container 96 MiB peak | under the 160 Mi limit |
| Init container copying talosctl (106 MB) to the emptyDir | chunked copy, fsync and page-cache drop per 8 MiB: cgroup peak 28 MiB at any limit (a plain copy fills whatever limit it is given) | limit 128 Mi |

Net cost to cp1: about 20 MiB resident, and a burst under 100 MiB for a few
seconds per sleep. The binary sits in a disk-backed emptyDir, not
`medium: Memory`: as tmpfs, its 106 MB would count as memory, and the copy was
OOM-killed at 64 Mi in that test. The first version used a plain copy. It finished in a local test
at 64 Mi, but on cp1 it was OOM-killed within a second at the first sync,
hence the chunked copy.

## Network

A CiliumNetworkPolicy allows only these destinations:

- the API server;
- cluster DNS;
- the nodes' Talos API (50000);
- port 443 outside the cluster (GitHub);
- port 16992 on private addresses (AMT).

The router admits only the control plane's address to the AMT network. Pod
traffic masquerades to that address.

## Wake-on-LAN

`send_wol` is there but disabled. It needs `WOL_MACS` and `WOL_BROADCAST`,
and the manifests set neither.

The amd64 ports carry the out-of-band network untagged and the runner VLAN
tagged. A powered-off NIC would have to see a magic packet broadcast on the
tagged VLAN. A pod can't emit an L2 broadcast without `hostNetwork`. Neither
has been proven. Turn Wake-on-LAN on only after a test from S5.

## Go-live (each step is Alex's call; nothing here is live until merged)

1. **Create the 1Password item** `zagreus-waker` with the fields above,
   before the merge. Without it the ExternalSecret fails and Argo shows
   `zagreus-waker` Degraded (harmless in observe mode, but noisy, and ESO
   keeps retrying against the vault the ARC key depends on).
2. **Merge in observe mode.** The heartbeat DaemonSet rolls once (one env
   var, an optional and absent volume, `set -f`), and writes nothing new
   until the waker does. The coredns PodDisruptionBudget is created. Argo
   starts ignoring `spec.maxRunners` on the three sets. The waker starts and
   writes nothing.
3. **Prove AMT power-on from S5 on one ProDesk** (needs Alex's word):
   - Shut the node down by hand with `talosctl -n <node> shutdown`.
   - Check that `kubectl -n zagreus-waker exec deploy/zagreus-waker --
     python3 /opt/waker/waker.py amt-status zag-amd64-02` prints `8`.
   - Then run `... amt-on zag-amd64-02`, and time it to Ready.
   - **Passed, 23 Oct 2026, 16:56–16:58 AEDT** (Alex's go). The run used
     the waker's own AMT client and the item's credentials:
     - `talosctl shutdown` as `os:operator` was accepted;
     - Ready went False after 7 s, and the kubelet Lease stopped;
     - AMT reported 8 (S5) 36 s after the shutdown;
     - 30 s later, `RequestPowerStateChange(2)` was accepted;
     - AMT reported on after 19 s, and the node was Ready 51 s after the
       power-on.
     - Talos uncordoned the node itself, and the evicted idle light runner
       came back on the arm builder.
4. **Deploy the asn-infra side:** the recording rule, the alert change and
   the Vector timing tweak. They must be live before step 6.
5. **`caps` mode** for a day. The caps track the nodes as they are, and
   cordoning a node by hand drops them. This is the waker-level proof that
   `maxRunners` 0 keeps jobs off sleeping nodes (see "Test plan").
6. **`full` mode** (Alex, 25 Oct 2026: "let's have it shut down nodes when it's been quiet for more than an hour and spin them up as load increases"). Pre-flight checks that day:
   - the waker pod's `os:operator` talosconfig reaches the Talos API on both nodes;
   - a dummy name written to the asleep list reached `talos:waker_asleep_by_node` in obs-metrics within 170 s, inside the 180 s publish lead.

   Alex answered the three open questions on 23 Oct 2026:
   - the idle timeout stays at 60 minutes;
   - core does not join as core (if the box becomes a cluster node, it joins
     under a new name, and gets listed in `waker.conf` then);
   - arm64 stays always-on, which means the waker manages the amd64 nodes
     only, as built.

**Rollback.** An Argo sync can't do this part. With `RespectIgnoreDifferences`,
every sync, manual ones included, keeps the live `maxRunners`. If you leave
it, sets the waker had at 0 stay at 0, and jobs that name a set
(`runs-on: zagreus-ci`, used by dependency-update PRs) queue for ever.

1. Set `mode` back to `observe`, or scale the waker to 0, and merge.
2. Power on any node that is asleep: `amt-on <node>` from the waker pod, or
   by hand. Wait for it to be Ready, then uncordon it if the waker's cordon
   is still on it. Then clear the asleep label, which only a waker in full
   mode removes: `kubectl label node <node> zagreus.asn.casa/asleep-`. Left on,
   `node-exporter` stays off that node (its power and resource metrics vanish).
3. Put each set back to its ceiling by hand:

   ```
   kubectl -n arc-runners patch autoscalingrunnerset zagreus-ci --type merge -p '{"spec":{"maxRunners":3}}'
   kubectl -n arc-runners patch autoscalingrunnerset zagreus-ci-x64 --type merge -p '{"spec":{"maxRunners":1}}'
   kubectl -n arc-builders patch autoscalingrunnerset zagreus-docker-amd64 --type merge -p '{"spec":{"maxRunners":1}}'
   kubectl -n arc-runners patch autoscalingrunnerset zagreus-simplytics-ci --type merge -p '{"spec":{"maxRunners":0}}'
   ```

   With no waker to share the pool, the slots stay with `zagreus-ci` and
   the Simplytics set goes to 0: its jobs, which name it, queue until it
   is given slots by hand (and `zagreus-ci` lowered by as many).

   The numbers are the `maxRunners` in each Application under
   `../runners/spritz/`. Check them there first.
4. If the waker is to go for good, also remove the `ignoreDifferences` and
   `RespectIgnoreDifferences` from those three Applications, so Argo owns
   `maxRunners` again.

The same applies if the waker dies or is deleted while in `caps` or `full`
mode: the sets keep whatever it last wrote.

## Queue watchdog

A job can sit queued inside GitHub with the node awake and the cap raised.
`timeout-minutes` cannot catch it (it counts from the job's start), and the
`held` rule above only keeps nodes awake. On 30 Oct 2026 two spritz release
jobs waited 58 and 61 minutes on `zagreus-docker-amd64`. The waker had
decided to wake zag-amd64-01 23 s after the jobs were queued, the node was
Ready 47 s later and the cap went 0 to 1 in the same second. The set's
listener restarted on that change and opened a fresh session, and GitHub's
broker answered every long poll for 56 minutes with nothing (`totalAssignedJobs=0`,
no message), then delivered the first job. ARC's listener reports only
assigned jobs, so nothing in the cluster could see the wait. (The builder's
floor, above, removes the cap change behind this. If a named-set job is held
anyway: `gh run cancel <run>`, then `gh run rerun <run> --failed` once
nothing else in the run is in progress, since a cancel stops every job in
the run. On 3 Nov the re-queued job was taken within a minute.) Earlier wakes
show the same shape at shorter lengths (first job 375 s to 2058 s after a wake
on 28 and 29 Oct, against the usual 89 s). Why GitHub held the jobs is not
known from the cluster side.

So the waker measures it from the GitHub API it already polls. Each pass that
reads the queue (caps and full modes) writes, for every driven set:

| Metric | Meaning |
|---|---|
| `zagreus_runner_oldest_queued_seconds{set}` | age of the oldest job GitHub lists as queued for the set; 0 when none |
| `zagreus_runner_queued_jobs{set}` | how many |
| `zagreus_runner_queue_sampled_timestamp_seconds` | when the waker last read the queue |

The jobs are counted with no grace and **orphans included** (the waker sets a
job aside from demand after the held window; it is still stuck). The gauges
travel as a node_exporter textfile: the waker patches the ConfigMap
`node-power/waker-metrics` (key `waker.prom`), `core/node-power-exporter.yaml`
mounts it on every node, and the metrics store scrapes it with the rest. No new
port, no router rule. Every node serves the same file, so a rule takes the max
over nodes. A pass that cannot read GitHub does not rewrite the file, so the
sample time ages: a blind watchdog reads as stale, not as a calm queue.

## Slot metrics

Alex, 1 Nov 2026: "the number of / which slots are in-use as a timeseries
for each node / aggregate". The waker already knows each node's state, its
configured slots, what it would offer now (room counted) and every runner
pod, so it writes them out each pass, GitHub readable or not (caps and full
modes), as a second file in the same ConfigMap (key `slots.prom`):

| Metric | Meaning |
|---|---|
| `zagreus_runner_node_state{runner_node,state}` | 1 for a managed node's current state: `awake`, `asleep`, `cordoned`, `not_ready` or `unknown` (unreadable this pass) |
| `zagreus_runner_slots_used{runner_node,set}` | runner pods of the set on the node, finished ones left out. Every set, light ones too, on every node with pods |
| `zagreus_runner_slots_offered{runner_node,set}` | what the waker would offer the set there now, its running runners included; 0 unless awake |
| `zagreus_runner_slots_configured{runner_node,set}` | the node's slots in `waker.conf`; for both sets of a shared pool, the pool's slots (the same slots, so never sum them) |
| `zagreus_runner_job_info{runner_node,set,runner,repo,workflow_run,job}` | 1 per runner pod running a job: which job is in which slot (driven sets only; their EphemeralRunners are the ones the waker reads) |
| `zagreus_runner_slots_sampled_timestamp_seconds` | when the waker last wrote the file |

Absence is never written as zero, and zero is never left as absence:

- A sleeping node is `state="asleep"` with 0 offered, not a missing series.
  (Only in `full` mode, which keeps the asleep list; in `caps` mode a
  powered-off node reads `not_ready`.)
- An unreadable node is `state="unknown"` and has no `offered` row.
- Every node with pods gets a `used` row for every set, so 0 is written
  as 0.
- A stopped waker leaves the sample time ageing.

The node label is `runner_node` because the scrape already sets `node` (the
exporter's own host). Every awake node serves the same file, so take `max by
(runner_node, set)`. Light slots are not modelled by the waker: their sets
are not driven, so only `used` covers them.
Gate on the sample time as well: the exporters keep serving a stopped waker's
last file, so without the gate stale numbers read as live.

## Asleep label

Argo's `zagreus-core` sync waits for every DaemonSet it applies to be
healthy ("waiting for healthy state of apps/DaemonSet/node-exporter"), and a
DaemonSet is healthy only when its pods are updated and available on every node
it counts. A sleeping node still counts, so its dead (Terminating or Pending)
pod kept the DaemonSet Progressing, the sync operation Running, and every later
revision of `zagreus-core` unapplied until someone terminated the operation or
woke the nodes (30 Oct 2026: 14:43 to 16:49 AEDT). It is also why `zagreus-core`
showed Progressing for hours at a time; no other zagreus app was affected (the
other twelve are Healthy, and the Talos-installed Cilium DaemonSets are in no
app).

The fix makes the DaemonSet stop counting the node, not Argo stop looking:

- The waker sets the label `zagreus.asn.casa/asleep=true` on a node in the same
  step that adds it to the asleep list (`write_asleep`, before the shutdown, so
  the pod is removed from a node that is still alive) and clears it when the
  node leaves the list (after its heartbeat pod is Ready), on a failed wake, and
  on a pass that finds it wrong (a restart). Full mode only. A failed patch is
  logged and retried; it never stops a sleep or a wake.
- `node-exporter` has a required node affinity `asleep DoesNotExist`.
- `kmsg-heartbeat` does **not**: the wake waits for its pod to be Ready on the
  node being woken, so a heartbeat that avoids the label would deadlock the
  wake. It is not affected today (its pods stay Ready through a sleep). A test
  pins both.

Other designs weighed: a Lua health override in `argocd-cm` can read only the
DaemonSet's own status, not Node objects, so it could only say "ignore
unavailable pods", which hides a real stuck rollout on an awake node. An
`ignore-healthcheck` annotation has the same blindness, and whether it governs
the sync wait or only the app's aggregate health is not something the deployed
Argo (v3.4.4) documents clearly. A NoSchedule/NoExecute taint would also work for
a DaemonSet but would need every DaemonSet that tolerates everything
(`operator: Exists`, as these do) rewritten to list its tolerations, and it
touches scheduling for every pod.

**Lesson: power-managed nodes count as unavailable to a DaemonSet
rollout.** The exporter's default `maxUnavailable: 1` stalled the rollout that
added the textfile collector: both sleeping amd64 nodes (NotReady) used up the
budget, so the awake nodes never got the new pods and the gauges were not
served. The DaemonSet now sets `maxUnavailable: "100%"`. Any DaemonSet on these
nodes whose rollout must reach the awake ones needs the same.

If any driven set's labels cannot be read, the pass publishes nothing: that
set's jobs would match nothing and its queue would read as empty. The sample
time then ages and the stale alert fires.

The alert rules live in asn-infra (`observability/rules/zagreus-runner-queue.yaml`).
There is no automatic recovery: whether bouncing the listener (a `maxRunners`
change) releases held jobs earlier is untested, and a wrong guess would
restart listeners under running jobs.

## Measurements

The waker records these in `zagreus-waker/waker-events`:

- `woke`, with `ready_seconds`;
- `first-job`, with `seconds` from the wake to the first driven runner pod
  Running on the node, which is wake-to-first-job latency;
- `slept`, with `awake_seconds`;
- failures.

Awake hours per day can also be read from the log store: one heartbeat per
minute per node that is on (`talos:kmsg_heartbeats_by_node`).

## Test plan

**Offline (done, in this PR):** the unit tests below, each mutation-proven,
and the asn-infra rule test (red on the old rules, green on the new).

**Red-first for "maxRunners 0 keeps jobs off sleeping nodes".** The red half
is already on record, and it isn't repeated live because the red state
strands a real job:

- **Red:** run 39882337983. `maxRunners: 1` with no schedulable node; a job
  was claimed and stuck while a classic runner sat idle.
- **Green, ARC level:** run 39883352061. `maxRunners: 0`; all five jobs went
  to the classic runner, and no EphemeralRunner was created.
- **Green, waker level (live, `caps` mode, no power, needs a go):**
  1. Cordon zag-amd64-01 and -02 by hand.
  2. Within one pass (30 s), the waker logs `maxRunners 3 -> 0` (and 1 -> 0
     twice), and `kubectl get autoscalingrunnersets -A` shows 0 for the three
     driven sets.
  3. Push a throwaway branch with a five-job matrix on
     `[self-hosted, linux, spritz-ci]`. Pass: all five run on classic runners,
     `kubectl -n arc-runners get ephemeralrunners -l
     actions.github.com/scale-set-name=zagreus-ci` stays empty, and the
     listener logs `max=0`.
  4. Uncordon both nodes. Within 30 s the caps are back to the ceilings.
  5. Clean up: delete the branch.
- **Wake and sleep (live, `full` mode, after the AMT proof):**
  1. With both nodes idle, set `idleMinutes` to 5 for the test. Watch
     02 sleep:
     - `waker-events` gets `slept`;
     - the asleep line shows in obs-logs (`{cluster="zagreus"} "log-pipeline
       asleep"`);
     - no `ZagreusNodeKmsgGone` fires;
     - AMT reports 8.
  2. Queue more jobs than the classic runners hold. Pass: 02 wakes, and
     `waker-events` gets `woke` and `first-job` (the wake-to-first-job
     latency).
  3. Stop the waker (`kubectl -n zagreus-waker scale deploy/zagreus-waker
     --replicas=0`) with a node asleep. Pass: `ZagreusNodeKmsgGone` fires for
     it within about 8 minutes (the 5-minute TTL, the 150 s window and the
     1-minute `for`). Scale it back up.

## Tests

```
python3 -m unittest discover -s cluster/zagreus/waker/tests -v
```

The pure decisions are tested directly. The procedures run against fakes of
the API server, GitHub, AMT and talosctl, which record every write in order.
That lets the tests assert *what happens before* the power goes off, for
example that the asleep line precedes the shutdown. The heartbeat test runs
the real shell loop from `kmsg-heartbeat.yaml`, extracted with `yq`, and
compares its output to `heartbeat_lines`.

The checks were run red-first: each mutation below makes the named tests fail.

| Mutation in `waker.py` | Tests that go red |
|---|---|
| sleeping nodes still count their slots (`node_awake` ignores asleep and Ready) | `Caps.test_every_driven_set_is_zero_while_the_amd64_nodes_sleep` |
| no cap drop before the cordon | `SleepProcedure.test_caps_drop_...`, `test_all_asleep_means_every_driven_set_claims_nothing` |
| the node never published asleep | the ordering test and 9 others |
| a claimed job counted twice | `Unmet.test_a_claimed_job_is_not_counted_twice` |
| observe mode allowed to patch | `Modes.test_observe_writes_nothing_at_all` |
| no renewal of the asleep list during a long sleep | `SleepProcedure.test_the_asleep_list_is_renewed_through_a_long_procedure` |
| a stale Ready trusted after a shutdown | `ReviewFindings.test_stale_ready_is_not_taken_back_without_amt_either` |
| a failed `talosctl` call always aborts | `ReviewFindings.test_a_failed_talosctl_call_on_a_node_going_down_is_a_sleep` |
| failed nodes never retried | `ReviewFindings.test_a_failed_wake_is_retried_after_the_backoff` |
| a node it can't wake is put to sleep | `ReviewFindings.test_a_node_it_cannot_wake_is_never_put_to_sleep` |
| no ETag, or no rate-limit hold, on GitHub calls | `GitHubClient.test_pages_and_then_revalidates`, `test_rate_limit_stops_calls_until_reset` |
| the asleep list never audited against AMT or the config | `test_a_listed_node_on_but_never_ready_leaves_the_list`, `test_unknown_names_are_dropped_from_the_list` |
| a set with no `maxRunners` left alone | `test_a_set_with_no_max_runners_is_written` |
| an unplaced runner ignored when choosing a sleep | `test_a_runner_not_yet_on_a_node_blocks_sleep` |
| no kubelet Lease check: a dead node counts while its Ready is stale, or for ever if the node controller is down (Codex P1, delta review M1/M2) | `CodexFindings.test_a_node_that_loses_power_stops_counting_at_once`, `test_an_off_node_whose_ready_never_flips_is_not_taken_back` |
| no limit on how long Ready can be "stale" (Codex P2) | `CodexFindings.test_a_crash_between_stamp_and_shutdown_does_not_strand_the_node` |
| no stamp clear when taking back an uncordoned listed node | `CodexFindings.test_a_listed_uncordoned_on_node_loses_its_stamp_when_taken_back` |
| Lease age from `spec.renewTime` (the worker's clock) | `LeaseFindings.test_a_worker_clock_far_behind_still_counts_a_live_node` |
| one failed Lease read drops the node (at the real 30 s cadence) | `LeaseFindings.test_one_failed_lease_read_does_not_drop_the_node` |
| a failed Lease read is trusted for ever | `LeaseFindings.test_a_lease_failing_for_long_drops_the_node` |
| no 30 s margin after the Lease stops, without AMT | `LeaseFindings.test_without_amt_off_waits_a_margin_after_the_lease_stops` |
| any manager's stamp counts, not just the kubelet's | `LeaseFindings.test_another_writer_on_the_lease_does_not_revive_a_dead_node` |
| without AMT, an unreadable Lease, or the shutdown stamp, confirms "off" | `LeaseFindings.test_without_amt_an_unreadable_lease_never_confirms_off`, `test_without_amt_a_hung_shutdown_with_a_live_kubelet_is_not_off` |
| the wake counts a Ready node with no live kubelet | `LeaseFindings.test_a_woken_node_without_a_live_kubelet_is_a_failed_wake` |
| the queue gauges never published | 6 of the `QueueWatchdog` tests (age, oldest of several, orphans included, drains to 0, sample time) |

Sharing (`tests/test_sharing.py`, 5 Nov 2026). Written first and seen red
(33 of the first 36 failed before the change; the three that passed are guards
that hold either way), then each mutation below run:

| Mutation in `waker.py` | Tests that go red |
|---|---|
| queued need never served (pool filled in `waker.conf` order only) | 9, among them `ShareRule.test_a_busy_queue_never_starves_the_other_to_zero` |
| fewest-held ignored | `test_a_busy_queue_never_starves_the_other_to_zero`, `test_two_queues_at_once_split_the_free_slots`, `Shared.test_two_repo_contention_on_awake_nodes_gives_each_a_share` |
| oldest-queue tie-break ignored | `test_two_queues_at_once_split_the_free_slots` |
| a pool set's ceiling ignored | `test_simplytics_stops_at_its_ceiling_and_spritz_gets_the_rest` |
| a pool's slots not shared per node | 11, among them `test_never_more_offered_than_the_awake_nodes_run` |
| leftover to the later pool set first | 7, among them both "Simplytics idle" spritz regressions |
| no hold of recently used slots | `test_recent_use_holds_a_slot_but_never_against_queued_jobs`, `test_sticky_slot_outlives_the_job_then_returns_to_spritz` |
| one unreadable queue holds every set, or blind sets may sleep | `test_an_unreadable_simplytics_queue_still_lets_spritz_wake_but_nothing_sleeps` |
| a backlog past its ceiling still wakes | `test_simplytics_at_its_ceiling_wakes_nothing_more` |
| an org set owns any repository | `Owns` (2) |
| claimed jobs counted as need | `ShareNeed.test_need_is_queued_jobs_not_yet_claimed_for_pool_sets_only` |
| RS256 without its padding; the token never reused | `AppToken` (one each) |
| demand read before the caps are applied (a slow GitHub delays a lower cap) | `Shared.test_caps_follow_the_nodes_before_github_is_read` |
| the second, share-moving apply counted as the next pass | `Shared.test_a_share_moved_after_demand_does_not_hasten_a_first_pass_lowering` |
| no fair-share cap (the refill race starves the waiting set) | `ShareRule.test_a_busy_set_is_held_to_its_share_so_a_freed_slot_is_not_reclaimed`, `test_the_cap_rule_holds_both_ways` |
| a blind queue keeps its last share | `Shared.test_a_blind_simplytics_queue_gives_its_share_back` |
| raises patched before lowers, or in the same pass as a lower in the pool | `Shared.test_a_slot_moving_between_sets_is_lowered_before_it_is_raised` |
| a never-read ceiling not held at the live cap | `Shared.test_a_pool_set_with_no_ceiling_ever_read_reserves_nothing` |
| public org repositories counted | `AppToken.test_org_queue_lists_the_installations_repos_of_that_org` |
| one blind queue withholds every queue's gauges | `Shared.test_a_blind_simplytics_queue_leaves_spritzs_queue_gauges_live` |

The heartbeat loop was also run under the pinned busybox, before and after
it was hardened. The old loop expanded an entry of `*` into 15 lines, crashed
on a renewal time with a leading zero (octal), and wrote all 12 of 12 names.
The new loop wrote 1, 1 and 8.
