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
subnet router, which SNATs, so PostgreSQL sees `10.10.20.103` — the router —
for every tailnet client, not deimos's own address. The `pg_hba` rule
therefore cannot single deimos out; the password is what does that.

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

```bash
kubectl --context admin@deimos -n data exec sts/postgres-standby -- \
  psql -U postgres -c "select pg_promote(wait => true)"
```

Then remove `SPRITZ_DATABASE_READONLY` from
`cluster/deimos/core/spritz-overrides.yaml` and let Argo roll the application.

Promotion is one-way. `standby.signal` is gone, the instance is a primary, and
the init container will not put it back — it asserts `standby.signal` only on a
data directory it seeded itself.

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
