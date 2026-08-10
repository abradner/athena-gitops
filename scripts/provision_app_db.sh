#!/usr/bin/env bash
# Provision a per-app, per-environment Postgres role (+ primary database) and
# store the generated credentials in 1Password — the source ExternalSecrets
# read from.
#
# Naming convention (Rails-style, applied to non-Rails apps too):
#   role = database = <app>_<environment>     e.g. kaff_production
#   1Password item  = <app>-<environment>     e.g. kaff-production
#     fields: database-username, database-password, database-url
#
# Usage:
#   ./scripts/provision_app_db.sh <app> <environment> [options]
#
#   ./scripts/provision_app_db.sh kaff production
#   ./scripts/provision_app_db.sh temporal_asn production --skip-db
#   ./scripts/provision_app_db.sh spritz staging --rotate
#
# Options:
#   --skip-db      Create only the role, no primary database. Use for
#                  temporal_* roles — temporal's schema tooling creates its
#                  own databases (via CREATEDB) with non-conventional names.
#   --rotate       Regenerate the password for an existing role and update
#                  the 1Password item in place.
#   --vault NAME   1Password vault (default: "Tooling - Athena").
#
# Connection (superuser) — from your local environment, never stored:
#   DB_SUPERUSER_URL   e.g. postgres://postgres:...@postgres.infra.asn.casa:5432/postgres
#   ...or the standard PG* vars: PGHOST, PGUSER, PGPASSWORD [, PGPORT]
#
# Idempotent: an existing role is left untouched (unless --rotate), an
# existing database is left untouched, and the 1Password item is
# created-or-updated. Safe to re-run per app as environments come online.
#
# The role gets CREATEDB so Rails db:prepare can create its aux databases
# (cache/queue/cable). Optional hardening once an app has fully booted:
#   psql "$DB_SUPERUSER_URL" -c 'ALTER ROLE <role> NOCREATEDB;'
set -euo pipefail

VAULT="Tooling - Athena"
SKIP_DB=false
ROTATE=false
POSITIONAL=()

while [ $# -gt 0 ]; do
  case "$1" in
    --skip-db) SKIP_DB=true; shift ;;
    --rotate)  ROTATE=true; shift ;;
    --vault)   VAULT="$2"; shift 2 ;;
    -h|--help) sed -n '2,35p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *)         POSITIONAL+=("$1"); shift ;;
  esac
done

if [ "${#POSITIONAL[@]}" -ne 2 ]; then
  echo "usage: $0 <app> <environment> [--skip-db] [--rotate] [--vault NAME]" >&2
  exit 64
fi

APP="${POSITIONAL[0]}"
ENVIRONMENT="${POSITIONAL[1]}"

case "$APP" in
  *[!a-z0-9_]*) echo "error: app must be lower_snake_case ([a-z0-9_])" >&2; exit 64 ;;
esac
case "$ENVIRONMENT" in
  production|staging) ;;
  *) echo "error: environment must be production or staging" >&2; exit 64 ;;
esac

ROLE="${APP}_${ENVIRONMENT}"
DB="$ROLE"
ITEM="$(echo "$APP" | tr '_' '-')-${ENVIRONMENT}"

# --- connection -------------------------------------------------------------
if [ -n "${DB_SUPERUSER_URL:-}" ]; then
  PSQL=(psql "$DB_SUPERUSER_URL")
  # host/port for the app's database-url field
  DB_HOST=$(echo "$DB_SUPERUSER_URL" | sed -E 's|^[^@]+@([^:/]+).*|\1|')
  DB_PORT=$(echo "$DB_SUPERUSER_URL" | sed -nE 's|^[^@]+@[^:/]+:([0-9]+).*|\1|p')
elif [ -n "${PGHOST:-}" ]; then
  PSQL=(psql)  # psql reads PGHOST/PGUSER/PGPASSWORD/PGPORT itself
  DB_HOST="$PGHOST"
  DB_PORT="${PGPORT:-}"
else
  echo "error: set DB_SUPERUSER_URL or PGHOST/PGUSER/PGPASSWORD" >&2
  exit 64
fi
DB_PORT="${DB_PORT:-5432}"

command -v op >/dev/null || { echo "error: 1Password CLI (op) not found" >&2; exit 69; }
op vault get "$VAULT" >/dev/null || { echo "error: cannot access vault '$VAULT' (op signin?)" >&2; exit 69; }

# --- role -------------------------------------------------------------------
# URL-safe alphanumeric password: it gets embedded in postgres:// URLs.
PASSWORD=$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 40)

ROLE_EXISTS=$("${PSQL[@]}" -Atc "SELECT 1 FROM pg_roles WHERE rolname = '$ROLE'" | head -1)

if [ "$ROLE_EXISTS" = "1" ] && [ "$ROTATE" = "false" ]; then
  echo "role $ROLE already exists — leaving password untouched (use --rotate to regenerate)"
  PASSWORD=""
elif [ "$ROLE_EXISTS" = "1" ]; then
  echo "rotating password for existing role $ROLE"
  # via stdin, not -c: keeps the password out of the process arg list
  "${PSQL[@]}" -q >/dev/null <<SQL
ALTER ROLE "$ROLE" WITH LOGIN CREATEDB PASSWORD '$PASSWORD';
SQL
else
  echo "creating role $ROLE (CREATEDB)"
  "${PSQL[@]}" -q >/dev/null <<SQL
CREATE ROLE "$ROLE" WITH LOGIN CREATEDB PASSWORD '$PASSWORD';
SQL
fi

# --- database ---------------------------------------------------------------
if [ "$SKIP_DB" = "false" ]; then
  DB_EXISTS=$("${PSQL[@]}" -Atc "SELECT 1 FROM pg_database WHERE datname = '$DB'" | head -1)
  if [ "$DB_EXISTS" = "1" ]; then
    echo "database $DB already exists — leaving it untouched"
  else
    echo "creating database $DB owned by $ROLE"
    "${PSQL[@]}" -q -c "CREATE DATABASE \"$DB\" OWNER \"$ROLE\";" >/dev/null
  fi
else
  echo "skipping database creation (--skip-db)"
fi

# --- 1Password --------------------------------------------------------------
if [ -z "$PASSWORD" ]; then
  echo "no new password generated — 1Password item '$ITEM' left as-is"
  echo "done."
  exit 0
fi

URL="postgres://${ROLE}:${PASSWORD}@${DB_HOST}:${DB_PORT}/${DB}"

if op item get "$ITEM" --vault "$VAULT" >/dev/null 2>&1; then
  echo "updating 1Password item '$ITEM' in vault '$VAULT'"
  op item edit "$ITEM" --vault "$VAULT" \
    "database-username[text]=$ROLE" \
    "database-password[password]=$PASSWORD" \
    "database-url[password]=$URL" >/dev/null
else
  echo "creating 1Password item '$ITEM' in vault '$VAULT'"
  op item create --category=login --title "$ITEM" --vault "$VAULT" \
    --tags athena-k8s \
    "database-username[text]=$ROLE" \
    "database-password[password]=$PASSWORD" \
    "database-url[password]=$URL" >/dev/null
fi

echo "done: role=$ROLE db=$([ "$SKIP_DB" = "true" ] && echo '(skipped)' || echo "$DB") item=$ITEM"
