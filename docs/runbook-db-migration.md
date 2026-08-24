# Runbook — migrating an app database onto the cluster Postgres

Moving a Rails app's database off its legacy per-app Postgres host onto the
cluster's shared Postgres, as part of taking the app off its LXC container.

Placeholders throughout — `<legacy-db-host>`, `<cluster-db-host>`,
`<edge-proxy-host>`, `<gateway-lb>`, `<app>`, `<vault>`. Concrete hosts, roles
and per-run figures live in the private infrastructure repo, not here.

**Status:** run end to end for one app's staging and production databases. Both
matched source row counts exactly; production cut over with no data loss and no
rollback needed. The traps below are the ones that actually bit during those
runs, not hypotheticals.

This is *not* an app's own `refresh_staging`-style script, if it has one. Those
refresh staging *from production* — a different operation, and one that should
guard against loading anything but a production dump. The safety mechanics here
are lifted from that pattern; the role-rename and environment-stamp steps are
not needed, because a migration keeps both the database name and the role name
the same on each side.

---

## 0. Preconditions

- `psql`, `pg_dump`, `op`, and (only if assets need checking) `aws` on PATH.
- 1Password unlocked. **`op whoami` reporting "account is not signed in" is a
  lie** — `op read` and `op item get` still work. Do not chase it.
- The app's Argo Application is Synced/Healthy and the target database already
  exists with its role (created by `scripts/provision_app_db.sh`).
- **Writes to the source must be stopped**, or anything written after the dump
  is lost. For staging this was free — the LXC was already shut down. For
  production this means a real maintenance window: stop the services *first*,
  then dump (see step 0a).
- **Writes to the target must be stopped too.** Easy to overlook, because the
  target is not serving public traffic yet — but its pods are running. The
  Temporal worker polls and can execute a workflow mid-load, and Argo can
  re-sync underneath you. A write landing between the drop and the reload is
  erased by the schema replacement or collides with it, and either way it
  exists in neither the source nor the rollback dump. Scale both deployments
  to zero before step 6:

  ```bash
  kubectl -n <ns> scale deploy/<app>-web deploy/<app>-temporal-worker --replicas=0
  kubectl -n <ns> rollout status deploy/<app>-web --timeout=60s
  ```

  Bring them back in step 8, which is also what applies any pending migrations.
  Note this makes the target briefly unavailable — harmless pre-cutover, and
  the reason the load should happen *before* traffic moves, never after.

## 0a. Freeze writes — both sides

Nothing below is safe while either end can write.

**Source (the LXC).** The `rails` user cannot sudo and the units are
system-level, so this needs root:

```bash
ssh root@<lxc-host> 'systemctl stop <app> <app>-temporal-worker'
ssh root@<lxc-host> 'systemctl is-active <app> <app>-temporal-worker'   # expect inactive
ssh root@<lxc-host> 'ps -eo args | grep -E "puma|solid-queue|temporal:run_worker" | grep -v grep'
```

Stop the **services**, not the container — that keeps the rollback hot. Solid
Queue counts: its dispatcher runs on a one-second loop inside the web service,
alongside a scheduler and worker. Leave any static-site unit running; it is the
rollback for the marketing hostname and touches no data.

Confirm no connections remain, ignoring your own psql:

```bash
psql "$SOURCE_URL" -Atc "select client_addr, application_name from pg_stat_activity
  where datname = '<db>' and pid <> pg_backend_pid()"
```

**Target (the cluster).** Scaling to zero is **not enough on its own.** Git
declares `replicas: 1` and the Application has `selfHeal: true`, so Argo
restores the pods within a reconcile cycle — during the dump or the load, which
is the exact race this step exists to prevent.

Suspend reconciliation first, and the **parent** Application too: the parent
manages this Application's spec and also self-heals, so disabling automated sync
on the child alone gets reverted.

```bash
# 1. suspend the parent, then the app's own Application
kubectl -n argocd patch application <parent-app> --type merge \
  -p '{"spec":{"syncPolicy":{"automated":null}}}'
kubectl -n argocd patch application <app>-<env> --type merge \
  -p '{"spec":{"syncPolicy":{"automated":null}}}'

# 2. now scale down
kubectl -n <ns> scale deploy/<app>-web deploy/<app>-temporal-worker --replicas=0
```

**Verify it stays down** rather than assuming — wait past one reconcile cycle,
then confirm both the replica count and that no sessions remain:

```bash
sleep 90
kubectl -n <ns> get deploy <app>-web <app>-temporal-worker \
  -o jsonpath='{range .items[*]}{.metadata.name}={.status.replicas}{"\n"}{end}'   # both empty/0
psql "$TARGET_URL" -Atc "select count(*) from pg_stat_activity
  where datname = current_database() and pid <> pg_backend_pid()"                 # expect 0
```

If either check is non-zero, stop — something is still reconciling and the load
would race it. Step 8 scales back up and restores both `syncPolicy` blocks.

## 1. Confirm what the source actually holds

Do **not** use `pg_stat_user_tables.n_live_tup`. On a server where autovacuum
has never analysed, every table reads `0` while holding data — this produced a
completely wrong "the source is empty, nothing to migrate" conclusion during
the staging run. Always `count(*)`:

```sql
SELECT '<table_a>='  ||(SELECT count(*) FROM <table_a>)
    ||' <table_b>='   ||(SELECT count(*) FROM <table_b>)
    ||' blobs='       ||(SELECT count(*) FROM active_storage_blobs)
    ||' migrations='  ||(SELECT count(*) FROM schema_migrations);
```

Record the numbers. They are the acceptance test in step 7.

## 2. Dump the source

```bash
mkdir -p db/dumps
DUMP="db/dumps/<db>-migrate-$(date +%F-%H%M).sql"
PGPASSWORD=... pg_dump -h <legacy-db-host> -U <role> -d <db> -f "$DUMP"
```

`$DUMP` is referenced by every step from here on — set it once, in this shell.

Plain text, no `--create`, no `--no-owner`. Ownership statements must survive:
stripping them silently leaves objects owned by whoever ran the load.

## 3. Validate the dump before dropping anything

These must **stop the run**, not just print. A validation that scrolls past
in a long terminal is no validation at all:

```bash
die() { echo "FAIL: $*" >&2; exit 1; }

[ -s "$DUMP" ] || die "dump missing or empty"
grep -qm1 "PostgreSQL database dump" "$DUMP"         || die "not a plain-text pg_dump"
grep -q "PostgreSQL database dump complete" "$DUMP"  || die "dump truncated"
! grep -qE "^(CREATE DATABASE|\\\\connect|DROP DATABASE)" "$DUMP" || die "dump carries database-level statements"
[ "$(grep -cE '^(INSERT INTO|COPY) ' "$DUMP")" -gt 0 ] || die "dump contains no data"
echo "dump validated: $DUMP"
```

A truncated dump loads "successfully" and restores only the tables that made
it into the file.

## 4. Check the roles the dump references — the one that bites

```bash
grep -oE "(OWNER TO|GRANT [A-Z, ]+ ON [^;]+ TO|REVOKE [A-Z, ]+ ON [^;]+ FROM|FOR ROLE) +\"?[A-Za-z0-9_]+\"?" \
  "$DUMP" | awk '{gsub(/"/,"",$NF); print $NF}' | sort -u | grep -vx PUBLIC

psql "$TARGET_URL" -Atc "select rolname from pg_roles"
```

Postgres treats a grant to a non-existent role as an error, and those
statements sit at the **very end** of the dump. Combined with
`--single-transaction`, the load gets all the way through every table and row
and *then* rolls the whole thing back.

A migration keeps the role name identical on both sides, so the app's own
role is never the problem. The ones that bite are *other* roles the source
server happens to host — a read-only reporting role, an admin role — which
appear in the dump's GRANTs but do not exist on the target.

Decide per role, and check rather than assume:

```bash
grep -cE 'OWNER TO "?<role>"?' "$DUMP"     # must be 0 for a role you intend to strip
```

If a missing role **owns** nothing, its grants are meaningless on the target
and can be stripped — loudly, never silently, because a vanished GRANT is
exactly what nobody notices until someone's read-only access is gone. If it
**does** own an object, stripping would leave that object owned by whoever ran
the load; create the role on the target instead.

Strip pattern — match the role **anywhere** in the statement, not just as the
trailing grantee. `ALTER DEFAULT PRIVILEGES FOR ROLE <admin-role> ... TO <reporting-role>`
names two roles and only one is the grantee:

```bash
STRIP='^(GRANT|REVOKE|ALTER DEFAULT PRIVILEGES).*(^|[^A-Za-z0-9_])(<admin-role>|<reporting-role>)([^A-Za-z0-9_]|$)'
grep -cE "$STRIP" "$DUMP"     # report the count; a vanished GRANT should never be silent
```

## 5. Guard the target, then back it up

Ask the database what it is. A hostname or a 1Password field can be edited; a
live connection cannot lie:

```bash
psql "$TARGET_URL" -Atc "select current_database() || ' ' || current_user"
```

Abort unless both match what you intend. Then take a rollback dump of the
target as it stands — cheap, and the only way back:

```bash
ROLLBACK="db/dumps/rollback-<db>-$(date +%F-%H%M).sql"
pg_dump "$TARGET_URL" -f "$ROLLBACK" || die "rollback dump failed — stop here"
grep -q "PostgreSQL database dump complete" "$ROLLBACK" || die "rollback dump truncated"
echo "rollback point: $ROLLBACK ($(du -h "$ROLLBACK" | cut -f1))"
```

Validate it with the same rigour as the source dump. It is described as the only
way back, so a partial file — a full disk, a dropped connection — must stop the
run *before* step 6, not be discovered after.

## 6. Load

Drop **every** schema, not just `public`. The staging target carried a
`public_legacy` schema the source did not have; left in place it would have
lingered forever.

**Build the load file first, then load it.** Do not stream a filter straight
into `psql`:

```bash
SCHEMAS=$(psql "$TARGET_URL" -Atc \
  "select nspname from pg_namespace where nspname not like 'pg\_%' and nspname <> 'information_schema'")

LOAD=$(mktemp)
{
  while IFS= read -r sch; do [ -n "$sch" ] && echo "DROP SCHEMA IF EXISTS \"$sch\" CASCADE;"; done <<< "$SCHEMAS"
  echo "CREATE SCHEMA public AUTHORIZATION \"<role>\";"
  "${strip_filter[@]}" "$DUMP" || exit 1
} > "$LOAD" || die "could not build the load file"

# the filtered result must still look like the dump it came from
grep -q "PostgreSQL database dump complete" "$LOAD" || die "filtered dump is truncated"
[ "$(grep -cE '^(INSERT INTO|COPY) ' "$LOAD")" -gt 0 ] || die "filtered dump carries no data"

psql "$TARGET_URL" -v ON_ERROR_STOP=1 --single-transaction -q -o /dev/null -f "$LOAD" \
  || die "load failed and was rolled back — target unchanged"
rm -f "$LOAD"
```

> **Why not a pipe.** If the filter cannot read the dump, or exits on an invalid
> regex, the braces still emit the `DROP`/`CREATE` statements. `psql` reads them,
> hits EOF, commits, and **returns 0** — the shell does not propagate a
> producer's failure, and `--single-transaction` only reacts to *SQL* errors, not
> to its input ending early. The result is a committed, empty database reported
> as a successful load. Materialising the file makes the failure visible before
> any transaction opens, and the two checks above catch a filter that silently
> produced less than it should.

where `strip_filter` is chosen explicitly, **never** left to an unset variable:

```bash
if [ -n "$STRIP" ]; then
  strip_filter=(grep -vE "$STRIP")
else
  strip_filter=(cat)
fi
```

> **Do not write `grep -vE "$STRIP" "$DUMP"` directly.** If `STRIP` is empty or
> unset, `grep -vE ""` matches every line and inverts to nothing — the load
> gets an empty input, drops every schema, and restores an empty database.
> `psql` exits 0. Every check in step 7 then reports zero rows, which reads as
> "the source was empty" rather than "the pipeline ate the dump". This is the
> same silent-success shape as the SIGPIPE trap below.

`ON_ERROR_STOP=1` plus `--single-transaction` means any failure is a no-op
rather than a half-loaded database.

> **Never pipe psql's stderr into `head` here.** Doing so cost a silent failed
> run during the staging migration: the `drop cascades to …` NOTICEs overflow
> `head`'s line budget, `head` exits, psql takes SIGPIPE mid-transaction, and
> everything rolls back — while the reported exit status still looked like
> success. Redirect stderr to a file and grep it afterwards.

No environment stamp is needed. `ar_internal_metadata.environment` already
holds the right value because source and target are the same environment.
(That step exists in `refresh_staging` only because it crosses environments.)

## 7. Verify

```bash
psql "$TARGET_URL" -Atc "select count(*) from pg_tables where schemaname='public'"
psql "$TARGET_URL" -Atc "select value from ar_internal_metadata where key='environment'"
psql "$TARGET_URL" -Atc "select count(*) from pg_class
   where relnamespace='public'::regnamespace and pg_get_userbyid(relowner) <> '<role>'"   -- must be 0
```

Then re-run the step-1 counts against **both** servers and diff them. They
must match on every table. Anything else means stop and investigate before
any traffic moves.

## 8. Restart the app, let Rails migrate

```bash
kubectl -n <ns> scale deploy/<app>-web deploy/<app>-temporal-worker --replicas=1
kubectl -n <ns> rollout status deploy/<app>-web --timeout=150s
```

(If you did not scale to zero in step 0a, `rollout restart` the two deployments
instead — but prefer the scale-down; see the target-writer precondition.)

The web entrypoint runs `db:prepare` on `rails server`, so any schema drift
between the dump and the deployed image applies on boot — the worker never
triggers it. Staging came across at 29 migrations against an image expecting
30, and `<a pending migration>` applied cleanly on restart. Confirm:

```bash
psql "$TARGET_URL" -Atc "select count(*) from schema_migrations"
```

## 9. Assets — usually nothing to do

Check before planning a sync. Both sides reach the **same Garage instance and
the same bucket** under different names:

```
LXC      GARAGE_ENDPOINT=https://<legacy-object-store-name>    -> HAProxy garage_3900 -> <object-store-host>:3900
cluster  GARAGE_ENDPOINT=http://<object-store-host>:3900 -> <object-store-host>
both     GARAGE_BUCKET=<app>-<env>
```

So blobs resolve without any object copying.

> **DNS actively contradicts this — do not use it to check.** `<object-store-host>`
> resolves to `<object-store-host>`, but `<legacy-object-store-name>` resolves only to
> Cloudflare (`<cdn-ip>`, `<cdn-ip>`, plus AAAA) with **no LAN A record
> at all**. Anyone confirming "same instance" by resolving both hostnames will
> conclude they are different backends and be wrong. The real evidence is the
> HAProxy backend (`garage_3900 → <object-store-host>:3900`) and object sampling.

Verify by sampling rather than assuming — 20 of 20 keys from the migrated
staging database were present:

```bash
while IFS= read -r k; do
  aws s3api head-object --bucket <bucket> --key "$k" \
    --endpoint-url http://<object-store-host>:3900 >/dev/null 2>&1 || echo "MISSING $k"
done < <(psql "$TARGET_URL" -Atc "select key from active_storage_blobs limit 20")
```

The bucket may hold **more** objects than the database references (staging:
187 objects against 53 blob rows, left over from an earlier prod refresh).
Harmless — orphaned storage, not missing data.

> zsh does not word-split unquoted variables. `for k in $KEYS` silently passes
> all keys as one string and every lookup "fails". Use a `while read` loop.

## 9a. `SECRET_KEY_BASE` — carry it across, or images 404

**This bit the staging migration and it will bite production harder.**

Page content in `page_versions.payload` stores *baked* ActiveStorage URLs:

```
/rails/active_storage/blobs/proxy/eyJfcmFpbHMiOnsiZGF0YSI6IjAxOWUyZmY4...--a7279abe35...
```

The trailing segment is an HMAC over the blob's UUID, signed with the
**source app's `SECRET_KEY_BASE`**. Migrating the database carries the URLs;
it does not carry the key. If the target app's key differs, every one of those
URLs fails verification and returns 404.

The signature is diagnostic. A failed lookup logs:

```
Completed 404 Not Found in 7ms (ActiveRecord: 0.0ms (0 queries, 0 cached))
```

**Zero queries** — Rails rejected the signature before it ever reached the
database. If you see missing images *with* queries, that is a genuinely absent
object; with zero queries it is always the key. Confirm by decoding the
payload (plain base64 JSON) and checking the UUID exists:

```bash
python3 -c "import base64;print(base64.b64decode('<payload-segment>').decode())"
psql "$TARGET_URL" -Atc "select key, service_name from active_storage_blobs where id='<uuid>'"
```

### Test it before cutover, without reading either secret

Take a signed URL out of the **source** database and throw it at the **target**
app through the Gateway. The exception class is the answer:

```bash
URL=$(psql "$SOURCE_URL" -tAc "select substring(payload::text from
  '/rails/active_storage/blobs/[a-z]+/[A-Za-z0-9+/=]+--[a-f0-9]+/[^\"]+')
  from page_versions where payload::text like '%active_storage%' limit 1")
curl -sS -o /dev/null -w '%{http_code}\n' --resolve <host>:443:<gateway-lb> "https://<host>$URL"
kubectl -n <ns> logs deploy/<app>-web --tail=40 | grep -E 'RecordNotFound|InvalidSignature'
```

| result | meaning |
|---|---|
| `ActiveRecord::RecordNotFound` | signature **verified** — keys match. (Expected before the data lands.) |
| `MessageVerifier::InvalidSignature`, or 404 in ~3ms with `0 queries` | keys **differ** — every stored URL will 404 |
| `200` + image bytes | keys match *and* the data and objects are in place |

This needs no access to either secret, which matters when reading them is
restricted. On the production run it returned `RecordNotFound` before the load
and `200 / <n> bytes / image/jpeg` after — end-to-end proof across key,
database, and object store in one request.

If the keys differ, copy the **live** value into the 1Password item and let ESO
resync before cutover. Rotating a key is a separate, deliberate decision —
doing it accidentally as part of a migration silently breaks every stored URL.

> Note the converse: once an environment's key is *deliberately* rerolled (as
> <app> staging's was, to stop sharing production's), URLs baked before the
> reroll are permanently unverifiable there. No migration fixes that — the data
> is fine, the URLs are not. The durable fix is app-side: store blob IDs and
> generate URLs at render time rather than baking signed URLs into content.

## 10. Smoke test

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://<host>/
curl -sS -o /dev/null -w '%{http_code}\n' https://<host>/up
kubectl -n <ns> logs deploy/<app>-web --tail=60 | grep -iE 'error|fatal|exception'
```

---

## Production-specific differences

1. **Production is atomic.** CT 132 runs *one* Rails app serving both
   `<canonical-host>` and `<app>.events` against one database. HAProxy splits
   them at the edge, but they write the same tables. Moving half sends RSVPs
   and EOI submissions to a different database from the one still being read —
   and the writes *succeed*, returning 422/302 with normal log lines. There is
   no error anywhere to notice.
2. **A write freeze is required.** Stop CT 132 before dumping. Any submission
   between dump and cutover is lost.
3. **`<admin-role>` and `<reporting-role>` must be stripped** (step 4), or the load rolls
   back after completing.
4. **Both `public` and `public_legacy` come across** — the production source
   has both.
5. Solid Queue counts as a writer. CT 132 ran a dispatcher on a 1-second
   loop plus a scheduler and worker inside `<app>.service`, and a separate
   `<app>-temporal-worker.service`. Stopping the **services** rather than the
   container freezes writes while keeping the rollback hot. `systemctl stop`
   leaves the worker unit in `failed` (the rake task exits non-zero on
   SIGTERM); that is cosmetic and `systemctl start` still restores it.

## Promoting an image from staging to production

Separate from the migration, and deliberately *not* done in the same change —
see the warning at the end.

**Promote a digest, not a tag.** GHCR tags are mutable and both deployments use
`imagePullPolicy: IfNotPresent`, so pinning a tag alone allows two failures: a
re-pushed tag gives production bytes staging never ran, and a node that already
cached the tag never re-pulls, leaving nodes on different code under one name.
Keep the tag alongside for readability — the digest is what resolves:

```yaml
image: <registry>/<app>:<image-tag>@sha256:<digest>
```

**Take the digest from what is running, not from the registry.** This is the
exact bytes that served staging:

```bash
kubectl -n <app>-staging get pod -l app=<app>-web \
  -o jsonpath='{.items[0].status.containerStatuses[0].imageID}'
```

**Preconditions, all of which should block the promotion:**

1. Staging's `web` and `temporal-worker` resolve to the **same** digest. If they
   differ, staging was never running one coherent build.
2. The migration delta is known and reviewed. Diff `schema_migrations` between
   the two databases:
   ```bash
   comm -23 <(psql "$STAGING_URL" -Atc "select version from schema_migrations order by version") \
            <(psql "$PROD_URL"    -Atc "select version from schema_migrations order by version")
   ```
   Then read each migration. Additive nullable columns are safe; anything that
   rewrites a table, adds a NOT NULL, or drops a column needs its own plan.
3. A pre-migration dump of production exists.

**The sharp edge: an image promotion is also a schema change.** The web
entrypoint runs `db:prepare` on `rails server`, so merging a promotion migrates
production on rollout, silently, as a side effect of what looks like a version
bump. The the first production run promotion carried exactly one migration —
`<AdditiveColumnMigration>`, an additive nullable column — which is why it was
safe to merge without a window.

**Never bundle a promotion with a database migration.** Moving a database to a
new server and changing its schema in one step means a failure cannot be
attributed to either, and the rollbacks are different (revert HAProxy vs revert
the image). Production was migrated on its existing pinned image, verified, and
only then promoted.

## Rollback

Before cutover: restore the step-5 rollback dump over the target using the
same step-6 procedure.

After cutover, rolling back is **not** simply the cutover in reverse, and two
things make it worse than it looks.

**First, decide what happens to writes the cluster has already accepted.** They
exist only in the target database; the frozen source has never seen them.
Rolling back discards them unless they are migrated the other way. Count them
before deciding — that number, not a preference, should drive the choice:

```bash
psql "$TARGET_URL" -Atc "select count(*) from <table_a> where created_at > '<cutover-time>'"
```

If it is zero, roll back freely. If it is not, either accept the loss
explicitly, or dump the target and reconcile into the source first. There is no
third option that keeps both.

**Second, order the steps so the two applications are never live together.**
Starting the legacy writers while the cluster still serves traffic gives two
apps writing two databases behind one hostname:

```bash
# 1. freeze the TARGET first — otherwise both sides accept writes
kubectl -n <ns> scale deploy/<app>-web deploy/<app>-temporal-worker --replicas=0
# (suspend the Applications as in step 0a, or selfHeal restores them)

# 2. bring the legacy app back up
ssh root@<lxc-host> 'systemctl start <app> <app>-temporal-worker'
ssh root@<lxc-host> 'systemctl is-active <app> <app>-temporal-worker'

# 3. confirm it actually serves, before any traffic depends on it
curl -sS -o /dev/null -w '%{http_code}\n' http://<lxc-ip>:3000/up

# 4. only then put traffic back
ssh root@<edge-proxy-host> 'cp /etc/haproxy/haproxy.cfg.bak-<stamp> /etc/haproxy/haproxy.cfg \
  && haproxy -c -f /etc/haproxy/haproxy.cfg && systemctl reload haproxy'
```

Steps 1–3 are a brief outage. That is the correct trade: a rollback that serves
nothing for thirty seconds beats one that splits writes across two databases and
has to be untangled afterwards.

The source database on <legacy-db-host> is never modified by this runbook, so it
still holds the frozen data — that is what makes rollback cheap. But it is only
cheap if the application in front of it is running before the traffic arrives.

Note the stopped worker unit may report `failed` rather than `inactive` (the
rake task exits non-zero on SIGTERM). That is cosmetic; `systemctl start`
still restores it.
