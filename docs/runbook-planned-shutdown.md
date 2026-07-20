# Runbook: Planned Cluster Shutdown & Restart

For a planned full-cluster power-down (rack move, electrical work, house
move). This is **not** disaster recovery — nothing is reinstalled and no
Talos configs are re-applied. Every node keeps its state on disk and the
cluster reassembles itself on boot. For rebuilds, see
[`bootstrap/README.md`](../bootstrap/README.md).

## Why this is safe by design

- Talos nodes boot from their installed disk; `talosctl shutdown` is a
  graceful power-off (pods stopped, filesystems unmounted, etcd stopped
  cleanly).
- etcd data persists on the control-plane disks. Quorum (2 of 3) re-forms
  automatically when the control-plane nodes boot — **no `talosctl
  bootstrap` is ever run on an existing cluster**.
- There is no distributed storage layer (no Longhorn/Ceph) to quiesce.
- Argo CD reconciles from `main` on boot. If `main` didn't change while
  the cluster was off, the desired state is unchanged — but Argo will
  still re-sync/re-apply as workloads restart and report health; expect
  sync activity, not literally nothing happening.

## What actually causes chaos on boot

The cluster's external dependencies, not the cluster itself:

| Dependency | Why it matters | Where it's configured |
| --- | --- | --- |
| **DHCP reservations** | Node network config is `network: {}` — all IPs come from DHCP. etcd peers and the kubeconfig reference the `10.10.80.x` addresses, so every node must come back with the *same* IP. | Router/DHCP server; addresses listed in `bootstrap/athena.zsh` |
| **DNS** | The kube API endpoint is `https://in.k8s.asn.casa:6443`. Nodes and `kubectl` need it to resolve at boot. | Wherever `asn.casa` is served |
| **NTP** | Raspberry Pi workers have no RTC; Talos gates the boot sequence on time sync. If NTP is unreachable, workers stall at boot. | Talos default (pool.ntp.org) → needs WAN |
| **Boot order** | Control plane must be up (and Cilium running) before workers try to join. | This runbook |

## Additional dependencies once PRs #3 / #4 land

PR #3 (root-app split, TLS at the Gateway, Argo CD self-management) and
PR #4 (mcg / kaff / spritz + shared Temporal) add to the boot-time picture:

- **External infra hosts become load-bearing.** The workloads depend on
  the managed Postgres (`postgres.infra.asn.casa` — Temporal, mcg, kaff,
  spritz), Garage S3 (`garage.infra.asn.casa:3900` — spritz), and HAProxy
  in front of everything. If those boxes are also moving racks, extend the
  ordering: shut the cluster down **before** Postgres (workloads write to
  it), and on the way up bring **network → infra hosts → cluster**.
  Workloads crashloop-then-recover if Postgres is late, but starting infra
  first avoids the noise.
- **Image pulls need WAN at boot.** The first-party apps run
  `ghcr.io/…:latest` with `imagePullPolicy: Always`, so after a reboot the
  kubelet contacts ghcr.io before starting each pod — *even when the image
  is cached*. No internet → pods stay in `ImagePullBackOff`. Worse,
  `:latest` means the cluster can come back running **newer code than it
  went down with** if an app repo merged meanwhile. The manifests already
  carry a TODO to pin `sha-` tags — do that (or freeze app-repo merges,
  not just this repo) before the move.
- **TLS is a non-issue for the move.** Gateway certificates live in
  Secrets (persisted in etcd); cert-manager only needs Cloudflare/WAN at
  renewal time, not at boot.
- **Extra verification targets:** the hello-world canary
  (`hello.bradner.net`) exercises gateway → TLS → route → pod → ESO in one
  check; then Temporal UI and the three apps.

### Rack move vs. planned reprovision — pick one at a time

PR #3 notes a **cluster reprovision is planned after it lands** (kubelet
`serverTLSBootstrap`, chart-based Argo CD). The move's downtime window is
a tempting time to do it, but they are different procedures: a move is
"power off, power on, touch nothing"; a reprovision is the DR playbook.
Decide **before** powering anything off:

- **Move only:** follow this runbook exactly. Note metrics-server will be
  broken after #3 merges until the reprovision happens (it now expects
  real kubelet certs) — that's unrelated to the move.
- **Move + reprovision:** do the shutdown half of this runbook (the etcd
  snapshot matters *more* here), move the hardware, then run the DR
  playbook in `bootstrap/README.md` instead of the startup half. Expect
  Let's Encrypt to reissue all Gateway certs on the fresh cluster — fine
  once, but use `letsencrypt-staging` if you end up iterating.

Don't decide mid-move, and don't reprovision a cluster you haven't first
confirmed boots healthy in its new home unless you accept DR as the only
path back.

## Before shutdown (while the cluster is still healthy)

1. **Verify your access tooling works now**, not after the move:

   ```bash
   source bootstrap/athena.zsh
   talosctl --nodes "$BOOTSTRAP_NODE" version
   kubectl get nodes
   ```

2. **Take an etcd snapshot** (cheap insurance; store it off-cluster, e.g.
   next to the Talos secrets note):

   ```bash
   source bootstrap/athena.zsh
   talosctl --nodes "$BOOTSTRAP_NODE" etcd snapshot athena-pre-move.snapshot
   ```

3. **Freeze GitOps input:** don't merge anything to `main` (here or in
   `pmn-workloads` — and once PR #4 lands, the mcg/kaff/spritz app repos,
   whose merges publish new `:latest` images) until the cluster is
   verified healthy again. Auto-sync with prune + self-heal means a merge
   queued while the cluster is down applies the moment it boots — keep
   the restart variable-free.

4. **Note the DHCP situation.** If the router/DHCP/DNS box is also moving,
   it must be up *before* any cluster node powers on, with reservations
   intact.

## Shutdown order

Workers first, then the control plane. No cordon/drain is needed for a
whole-cluster shutdown — graceful node shutdown stops pods anyway, and
there is nowhere to reschedule them.

```bash
source bootstrap/athena.zsh

# 1. Workers
for ip in "${WORKER_IP[@]}"; do
  talosctl --nodes "$ip" shutdown
done

# 2. Control plane — all three back-to-back, so etcd members stop together
for ip in "${CONTROL_PLANE_IP[@]}"; do
  talosctl --nodes "$ip" shutdown
done
```

3. Power off the hypervisor host(s) after the control-plane VMs are down.
4. Network gear last.

## Startup order

1. **Network first:** switch, then router/DHCP/DNS. Before powering any
   node, confirm from a laptop that DHCP works and
   `in.k8s.asn.casa` resolves.
2. **Control plane:** boot the hypervisor, then all three control-plane
   VMs (check they're set to autostart, or start them manually). etcd
   re-forms quorum once 2 of 3 are up. **Do not run `talosctl bootstrap`,
   `apply-control.sh`, or `provision.sh`** — the cluster already exists.
3. **Wait for the control plane to be healthy:**

   ```bash
   source bootstrap/athena.zsh
   talosctl --nodes "${CONTROL_PLANE_IP[@]}" health
   kubectl get nodes   # control-plane nodes Ready once Cilium is up
   ```

4. **Workers:** power on the Pis. They rejoin on their own — no
   `apply-worker.sh`.
5. **Verify:**

   ```bash
   kubectl get nodes                 # all Ready
   kubectl get pods -A               # nothing crash-looping
   kubectl get applications -n argocd  # Synced / Healthy
   ```

   Check a Gateway route (Argo CD / Headlamp UI) to confirm Cilium's L2
   announcements resumed.

## Troubleshooting

- **A node came up with the wrong IP** → fix the DHCP reservation and
  reboot that node. Don't re-apply Talos configs to work around it.
- **Workers stuck booting** → almost always NTP (no WAN yet) or the
  control plane not Ready yet. Fix the dependency; the node continues on
  its own.
- **Only 2 of 3 control-plane nodes return** → the cluster is functional
  (quorum holds); fix the third promptly, but nothing else is urgent.
- **etcd genuinely lost** (multiple control-plane disks dead) → that's a
  disaster-recovery scenario: restore from the snapshot taken above
  (`talosctl etcd snapshot` docs) or rebuild via
  [`bootstrap/README.md`](../bootstrap/README.md).

## Never do during a planned move

- `talosctl reset` — wipes the node's disk. Recovery only.
- `talosctl bootstrap` — creates a *new* etcd cluster; running it against
  existing members is destructive.
- Re-running `apply-control.sh` / `apply-worker.sh` / `provision.sh` —
  configs are already on disk; these are for provisioning.
