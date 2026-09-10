#!/usr/bin/env bash
# Provision the PostgreSQL replication role the deimos standby streams as, and
# store its password in 1Password — the source the ExternalSecret reads from.
#
# Sibling of provision_app_db.sh, and deliberately shaped like it: same
# superuser connection, same 1Password conventions, idempotent, safe to re-run.
#
# WHAT THIS DOES *NOT* DO: edit pg_hba.conf. PostgreSQL has no SQL interface for
# it, so that one step is by hand on the primary host and is printed at the end.
#
# Usage:
#   ./scripts/provision_replication_role.sh
#   ./scripts/provision_replication_role.sh --rotate
#
# Options:
#   --rotate       Regenerate the password and update 1Password in place.
#                  The standby then needs its Secret refreshed and its pod
#                  restarted; see docs/runbook-deimos-data-tier.md.
#   --vault NAME   1Password vault (default: "Tooling - Athena").
#
# Connection (superuser) — from your local environment, never stored:
#   DB_SUPERUSER_URL   e.g. postgres://postgres:...@postgres.infra.asn.casa:5432/postgres
#   ...or the standard PG* vars: PGHOST, PGUSER, PGPASSWORD [, PGPORT]
set -euo pipefail

VAULT="Tooling - Athena"
ROTATE=false
ROLE="replicator_deimos"
ITEM="deimos-postgres-replication"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --rotate) ROTATE=true; shift ;;
    --vault)  VAULT="$2"; shift 2 ;;
    -h|--help) sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 64 ;;
  esac
done

command -v psql >/dev/null || { echo "psql not found" >&2; exit 69; }
command -v op   >/dev/null || { echo "1Password CLI (op) not found" >&2; exit 69; }

if [[ -n "${DB_SUPERUSER_URL:-}" ]]; then
  PSQL=(psql "$DB_SUPERUSER_URL" --no-psqlrc --tuples-only --no-align --quiet)
else
  : "${PGHOST:?set DB_SUPERUSER_URL or PGHOST/PGUSER/PGPASSWORD}"
  PSQL=(psql --no-psqlrc --tuples-only --no-align --quiet)
fi

exists="$("${PSQL[@]}" -c "select 1 from pg_roles where rolname = '${ROLE}'")"

if [[ -n "$exists" && "$ROTATE" == false ]]; then
  echo "role ${ROLE} already exists; pass --rotate to change its password"
else
  # 32 bytes of base64 with the URL-unsafe characters removed, so the value
  # survives being pasted into a connection string or a passfile unquoted.
  password="$(LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 40)"

  if [[ -n "$exists" ]]; then
    "${PSQL[@]}" -c "alter role ${ROLE} with password '${password}'"
    echo "rotated password for ${ROLE}"
  else
    "${PSQL[@]}" -c "create role ${ROLE} with login replication password '${password}'"
    echo "created role ${ROLE}"
  fi

  # The standby's init container asks pg_replication_slots whether its slot is
  # still usable before deciding to re-seed. Without this grant that query
  # returns nothing, which is indistinguishable from "the slot is gone" and
  # would make it re-seed on every single start.
  "${PSQL[@]}" -c "grant pg_read_all_stats to ${ROLE}"

  if op item get "$ITEM" --vault "$VAULT" >/dev/null 2>&1; then
    op item edit "$ITEM" --vault "$VAULT" \
      "username[text]=${ROLE}" "password[password]=${password}" >/dev/null
    echo "updated 1Password item ${ITEM}"
  else
    op item create --category=Database --vault "$VAULT" --title "$ITEM" \
      "username[text]=${ROLE}" "password[password]=${password}" >/dev/null
    echo "created 1Password item ${ITEM}"
  fi
fi

# A slot that nobody is consuming pins WAL on the primary forever. The zone is
# deliberately stopped for weeks at a time between the rehearsal and the
# wedding, so "forever" here is a filled disk on the primary — the one way this
# whole arrangement could take down the site it exists to protect.
#
# 4 GB instead: past that the slot is invalidated, the standby notices on its
# next start and takes a fresh base backup. The cluster is ~300 MB, so that
# costs seconds and nothing else.
current="$("${PSQL[@]}" -c "select setting from pg_settings where name = 'max_slot_wal_keep_size'")"
if [[ "$current" == "-1" ]]; then
  "${PSQL[@]}" -c "alter system set max_slot_wal_keep_size = '4GB'"
  "${PSQL[@]}" -c "select pg_reload_conf()" >/dev/null
  echo "set max_slot_wal_keep_size = 4GB (was unlimited)"
else
  echo "max_slot_wal_keep_size already bounded at ${current}"
fi

cat <<EOF

Remaining step, on the primary host — psql cannot do this one:

  1. Add to pg_hba.conf, above any 'reject' lines:

       # deimos standby. The source address is the Tailscale subnet router,
       # not deimos itself, because it SNATs; every tailnet client reaches
       # PostgreSQL as this address. The password is what actually
       # distinguishes deimos here.
       host  replication  ${ROLE}  10.10.20.103/32  scram-sha-256
       host  postgres     ${ROLE}  10.10.20.103/32  scram-sha-256

     The second line is not redundant: the standby's init container makes an
     ordinary connection to ask whether its replication slot is still valid,
     and 'host replication' does not cover ordinary connections.

  2. Reload — this is a reload, not a restart, and does not drop connections:

       psql -c 'select pg_reload_conf()'

Nothing else on the primary needs changing. Measured 2026-09-11 it already
runs wal_level=replica, max_wal_senders=10, max_replication_slots=10 and
hot_standby=on, so no restart is required at any point.
EOF
