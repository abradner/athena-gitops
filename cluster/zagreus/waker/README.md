# The zagreus node waker

One pod on the control plane. It keeps zagreus's amd64 runner nodes asleep
until the classic runners can't keep up, wakes them through Intel AMT, and
puts them back to sleep after an idle hour. It also sets each zagreus scale
set's `maxRunners` to the slots on the nodes that are awake.

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

**Demand** comes from GitHub's jobs API. The waker reads every queued job in
spritz's queued and in-progress runs from the last 72 hours. A job is demand
for a driven set when the set carries every label the job asks for:

- If the job names the set (`runs-on: zagreus-ci`), it counts at once. No
  classic runner carries a set's name, so only zagreus can run it.
- If the job asks for shared class labels, it counts once it has been queued
  for `saturationGraceSeconds` (60). An idle classic runner with those labels
  takes a job within seconds, so a job still waiting after a minute has no
  classic runner free.

That age test stands in for "no idle classic runner matches", so the waker
needs no runners API and no Administration permission. The token is a
fine-grained one with **Actions: read on spritz only**.

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

## Wake

The trigger is unmet demand for a set, with a sleeping node that has a slot
for it. One wake at a time, then a 180 s cooldown. Nodes are taken in
`waker.conf` order. The steps:

1. AMT `RequestPowerStateChange(2)` over WS-MAN (HTTP digest, port 16992).
   Wake-on-LAN is the fallback when it is configured (see "Wake-on-LAN").
2. Wait for AMT to report on, then for the Node to be Ready (up to 10 min).
3. Wait for that node's heartbeat pod to be Ready, then take the node off the
   asleep list. Its heartbeats are flowing by then, and the old assertion
   stays in the alert's 150 s lookback while the first ones arrive.
4. Uncordon the node and raise the caps.

If any step fails, the node comes off the asleep list. Its missing heartbeat
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

## Slots

`waker.conf` gives each node its slots per set. Wake order follows the list:

| Node | Memory (allocatable) | `zagreus-ci` | `zagreus-ci-x64` | `zagreus-docker-amd64` |
|---|---|---|---|---|
| zag-amd64-01 | 22.5 GiB | 1 | 1 | 1 |
| zag-amd64-02 | 14.5 GiB | 2 | 0 | 0 |
| **both** | | **3** | **1** | **1** (= the #223 ceilings) |

A node's slots must fit on it even when everything else that can land there
does. That means two light pods (768 MiB each), the bundle fill and coredns.
A heavy pod requests 6 GiB. The builder requests 3 GiB plus its 768 MiB dind.

- zag-amd64-01 alone carries two heavy pods and the builder: 12 + 3.75 + 2.25
  = 18 GiB of 22.5.
- zag-amd64-02 alone carries two heavy pods: 12 + 2.25 = 14.25 GiB of 14.5.
  The builder's slot belongs to 01, so it never lands on 02 when 02 is the
  only node awake.

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

## Credentials (ESO, control plane only)

The 1Password item `zagreus-waker` sits in the CI-only vault. `waker.yaml`
lists its fields: the GitHub token (Actions: read, spritz), the AMT login, a
`node=address` map for AMT, and an `os:operator` talosconfig.

- The Secret is mounted only into this pod, which is pinned to the control
  plane. A worker's kubelet can't read it.
- AMT addresses stay in the vault, not in this repo.
- `os:operator` can reboot and shut down any zagreus node, and nothing
  outside zagreus: its certificate comes from zagreus's own Talos CA.

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
| Init container copying talosctl (106 MB) to the emptyDir | python 13 MiB; the cgroup fills to its 64 Mi limit with reclaimable page cache, and the copy completes | limit 64 Mi |

Net cost to cp1: about 20 MiB resident, and a burst under 100 MiB for a few
seconds per sleep. The binary sits in a disk-backed emptyDir, not
`medium: Memory`: as tmpfs, its 106 MB would count as memory, and the copy was
OOM-killed at 64 Mi in that test.

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

1. **Merge in observe mode.** This changes the heartbeat DaemonSet: it rolls
   once, adds one env var and an optional, empty volume, and writes
   nothing new until the waker does. It also adds the `ignoreDifferences` and
   starts the waker, which writes nothing in this mode.
2. **Create the 1Password item** `zagreus-waker` with the fields above.
3. **Prove AMT power-on from S5 on one ProDesk** (needs Alex's word):
   - `talosctl -n <node> shutdown` the node by hand, then check that
     `kubectl -n zagreus-waker exec deploy/zagreus-waker -- python3
     /opt/waker/waker.py amt-status zag-amd64-02` prints `8`.
   - Then run `... amt-on zag-amd64-02`, and time it to Ready.
   - Until this passes, AMT is only presumed to work from S5.
4. **Deploy the asn-infra side:** the recording rule, the alert change and
   the Vector timing tweak. They must be live before step 6.
5. **`caps` mode** for a day. The caps track the nodes as they are, and
   cordoning a node by hand drops them. This is the "maxRunners 0 keeps jobs
   off sleeping nodes" proof in the test plan.
6. **`full` mode**, once Alex has answered the open questions: the idle
   timeout, whether core joins, and whether arm64 stays always-on.

**Rollback:** set `mode` back to `observe` and merge, then put the
Applications' ceilings back with an Argo sync. If nodes are asleep, power
them on by hand with `amt-on`.

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
  4. Uncordon both nodes. Within 30 s the caps are back to 3/1/1.
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
