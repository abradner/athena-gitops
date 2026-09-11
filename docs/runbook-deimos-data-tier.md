# Runbook — the deimos data tier

Two PostgreSQL instances run in the `data` namespace of the deimos zone, and
they are not variations on each other:

| | `postgres-standby` | `postgres-solid` |
|---|---|---|
| What it is | Physical streaming standby of `postgres.infra.asn.casa` | An empty local instance |
| Writable | No, ever, until promoted | Yes |
| Holds | Every database the primary has | `spritz_production_cache`, `_queue`, `_cable` |
| If the volume is lost | Re-seeds from the primary automatically | Rails recreates it on next boot |
| Worth backing up | No — the primary is the backup | No |

**The Solid Queue worker is not deployed in this zone.** It is excluded from
the deimos Application rather than scaled to zero, so nothing can drift it back
up. Jobs still enqueue — the Solid Queue database here is writable — and simply
accumulate unprocessed until a promotion. A worker running against a read-only
primary would claim jobs and then fail on the first write, and claimed-then-
failed is worse than never claimed: the primary's own worker would never see
them. The Temporal worker is excluded for a simpler reason: there is no
Temporal server in this zone for it to talk to.

The split exists because Rails writes to the Solid databases on ordinary guest
traffic. Rack::Attack's throttle counters go through `Rails.cache`, which is
Solid Cache, which is a table — so with everything on a read-only standby,
*browsing the site* fails, not merely submitting an RSVP.

## Standing up the replication, first time

Three steps, and only the second touches the primary.

**1. Create the role and store its password.** From a machine with the
superuser credentials and `op` signed in:

```bash
DB_SUPERUSER_URL='postgres://postgres:...@postgres.infra.asn.casa:5432/postgres' ./scripts/provision_replication_role.sh
```

This creates `replicator_deimos`, grants it `pg_read_all_stats`, writes the
password to the 1Password item `deimos-postgres-replication`, and bounds
`max_slot_wal_keep_size` — see "Parking the zone" below for why that last one
is not optional.

**2. Allow it in `pg_hba.conf`,** on the primary host. The script prints the
exact two lines and the reload command. It is a reload, not a restart, and
drops no connections.

Note what the source address is. deimos reaches the lab through the Tailscale
subnet router, which SNATs, so PostgreSQL sees **the router's address** for
every tailnet client, not deimos's own. The `pg_hba` rule therefore cannot
single deimos out; the password is what does that.

The address itself is in `asn-infra` and deliberately not written down here —
this repository is public. Confirm it rather than assuming, from a pod in the
zone: `select inet_client_addr()`.

**3. Let Argo sync.** The ExternalSecret pulls the credentials, the init
container takes a base backup, and the standby starts. The cluster is around
300 MB, so this takes seconds rather than minutes.

## Checking it is actually replicating

From the primary:

```bash
psql -c "select application_name, state, sync_state, replay_lag from pg_stat_replication"
```

`state = streaming` and a `replay_lag` under a second is the healthy answer.
An empty result means nothing is connected.

From deimos:

```bash
kubectl --context admin@deimos -n data exec sts/postgres-standby -- \
  psql -U postgres -c "select pg_is_in_recovery(), pg_last_wal_replay_lsn()"
```

`pg_is_in_recovery()` must be `t`. If it is `f`, this standby has been promoted
— deliberately or otherwise — and is now diverging from the primary. That is
the condition in "Two writable databases" below.

## Parking the zone, and the one way it can hurt the primary

The zone is stopped between the rehearsal and the wedding week. While it is
stopped, its replication slot is inactive but still present, and an inactive
slot pins WAL on the primary indefinitely. Left unbounded, a fortnight of
parking fills the primary's disk — the standby taking down the site it exists
to protect.

`max_slot_wal_keep_size = 4GB` is what prevents that. Past 4 GB the primary
invalidates the slot instead of retaining more. The standby's init container
checks the slot's `wal_status` on every start, sees `lost`, and takes a fresh
base backup. Nothing is lost, because the standby holds nothing of its own.

So the sequence after a long park is expected to be: start the instance, the
standby re-seeds, replication resumes. If it resumes without re-seeding, it
simply caught up from retained WAL — also fine.

## When the primary is unreachable

This is the case the zone exists for, and nothing needs doing.

The init container distinguishes "the primary says our slot is gone" from "we
cannot reach the primary at all", and only the first triggers a re-seed. If the
primary is unreachable the standby starts from the data it has and serves
reads, retrying the connection in the background. It will catch up on its own
when the link returns.

## Promotion — the escalation, not the default

Promote only when the primary is unreachable on **both** the public and mesh
paths, sustained, with no expectation of return. On the wedding day, lean
towards promoting: the day-of features need writes.

**Do these in order. Step 1 is not optional and is not a tidy-up.**

**1. Suspend the object mirror, before anything becomes writable.**

```bash
kubectl --context admin@deimos -n data patch cronjob garage-mirror \
  -p '{"spec":{"suspend":true}}'
```

`rclone sync` treats the primary as the source of truth and deletes anything on
the destination the source does not have. The moment the application here can
write, every upload is deimos-only — and the next mirror run, at most fifteen
minutes later, would delete it irrecoverably. Suspending afterwards is too
late: the window is the whole promoted interval.

**2. Promote the database.**

```bash
kubectl --context admin@deimos -n data exec sts/postgres-standby -- \
  psql -U postgres -c "select pg_promote(wait => true)"
```

**3. Turn off read-only mode.** Remove `SPRITZ_DATABASE_READONLY` from
`cluster/deimos/core/spritz-overrides.yaml` and let Argo roll the application.

**4. Start the job worker.** In
`cluster/deimos/apps/spritz-production-spritz.yaml`, change the `exclude` line
from:

```yaml
      exclude: '{solid-queue.yaml,temporal-worker.yaml}'
```

to exactly:

```yaml
      exclude: temporal-worker.yaml
```

Note the braces go too — a brace list of one is not worth relying on. The
Temporal worker stays excluded either way; there is still no Temporal server in
this zone.

Until this point jobs have been accumulating unprocessed in the local Solid
Queue database, which is deliberate: a worker running while the primary was
read-only would have claimed them and then failed on the first write, and
claimed-then-failed is worse than never claimed — the primary's own worker
never sees them again.

Once Argo syncs, the backlog drains against the newly writable database. Watch
it rather than assuming:

```bash
kubectl --context admin@deimos -n spritz-production logs -f deploy/spritz-solid-queue
```

Promotion is one-way. `standby.signal` is gone, the instance is a primary, and
the init container will **not** put it back: it writes that file only inside
the seed path, on a data directory it built itself. Recreating it on a later
pod start — a node reboot, a liveness restart, a rollout — would silently turn
the promoted, authoritative database back into a standby of a primary it has
since diverged from.

## Two writable databases

Once promoted, deimos accepts writes. If the old primary ever accepts a write
after that moment, the two have diverged, and PostgreSQL offers nothing to
merge them.

Mostly this cannot happen, and for a structural reason worth stating: a zone
with no internet cannot receive traffic, so it cannot accept writes. An
islanded primary is offline, not divergent.

The case that remains is the watchdog scaling deimos up because *it* cannot
see the primary while Cloudflare still can. Hence: connectors scaling up is not
the dangerous act — promotion is. Keep those two decisions separate, and let
only the second need a human.

If it happens anyway, pick a winner and rebuild the loser; do not attempt a
merge. The winner is whichever zone served most recently, which after a
promotion is deimos. Before rebuilding the loser, check whether it accepted any
writes during the window — for this application that means RSVP submissions,
and the volume is small enough to extract a handful of rows by hand.
`pg_basebackup` will not ask.

## Rotating the replication password

```bash
DB_SUPERUSER_URL='...' ./scripts/provision_replication_role.sh --rotate
kubectl --context admin@deimos -n data annotate externalsecret postgres-replication \
  force-sync="$(date +%s)" --overwrite
kubectl --context admin@deimos -n data rollout restart sts/postgres-standby
```

The restart is needed because the passfile is written by the init container
from the Secret; the running instance does not re-read it.

## Object storage

`garage` in the same namespace is a **mirror**, not a cluster member. It holds
a one-way copy of the primary's `spritz-production` bucket, pulled by an
`rclone sync` CronJob every fifteen minutes, and nothing written to it
propagates back.

That was a deliberate trade. Joining the primary's Garage cluster would need
`rpc_public_addr` changed from `127.0.0.1:3901` — as it stands no second node
can connect at all — and the replication factor raised, each requiring a
restart of the live service, and it would stretch a storage cluster across the
same WAN link whose failure is the reason this zone exists. The objects are
immutable and uniquely keyed, so a one-way copy loses nothing but recency.

The node is given the *primary's own access key*, imported with the same id and
secret, so the application's credentials work unchanged and only
`GARAGE_ENDPOINT` differs between zones.

### Checking the mirror

```bash
kubectl --context admin@deimos -n data get cronjob garage-mirror
kubectl --context admin@deimos -n data logs -l job-name --tail=20 --prefix
```

A run that fails while the primary is unreachable is expected and needs no
action; the next run is fifteen minutes later.

### Forcing a sync now

```bash
kubectl --context admin@deimos -n data create job --from=cronjob/garage-mirror mirror-now
```

### If the bootstrap looks wrong

The `bootstrap` sidecar re-checks the layout, key, bucket and permissions every
hour and repairs whatever is missing, so the usual fix is to wait or restart
the pod. Its log says what it did. `/health` on the admin port returns 503
until the layout is applied, which is what the readiness probe reads — a
`garage` pod stuck `0/2` Ready almost always means the layout never applied.

### After a promotion

The application starts writing objects to this node, and they exist nowhere
else. They survive only because the mirror was suspended as step 1 of the
promotion procedure — if it was not, they are already gone.

When the primary returns, copy those objects back to it before resuming the
mirror, then unsuspend:

```bash
kubectl --context admin@deimos -n data patch cronjob garage-mirror \
  -p '{"spec":{"suspend":false}}'
```

This is the same shape as the database reconciliation above: pick a winner,
copy by hand, do not merge.
