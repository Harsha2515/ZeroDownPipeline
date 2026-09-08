#!/usr/bin/env bash
# Restore a backup from S3 into the primary.
#
# Destructive, so it refuses to run without --confirm. With no key argument it
# reads backups/latest.json - the pointer backup_db.sh maintains - rather than
# sorting object listings and hoping.
#
# Usage:
#   restore_db.sh --confirm                          # restore the latest backup
#   restore_db.sh --confirm backups/mysql/../x.sql.gz  # restore a specific key

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib.sh"

require_cmd docker aws gzip
load_env "${ZDP_ROOT}/.env"
require_env MYSQL_ROOT_PASSWORD MYSQL_DATABASE S3_BUCKET

CONFIRM=0
KEY=""
for arg in "$@"; do
  case "$arg" in
    --confirm) CONFIRM=1 ;;
    *) KEY="$arg" ;;
  esac
done

if (( CONFIRM == 0 )); then
  err "restore_db.sh OVERWRITES the ${MYSQL_DATABASE} database on mysql-primary."
  die "re-run with --confirm if that is what you want"
fi

if [[ -z "$KEY" ]]; then
  log "no key given - reading s3://${S3_BUCKET}/backups/latest.json"
  KEY="$(aws s3 cp "s3://${S3_BUCKET}/backups/latest.json" - 2>/dev/null \
        | grep -oE '"key"[[:space:]]*:[[:space:]]*"[^"]+"' | cut -d'"' -f4 || true)"
  [[ -n "$KEY" ]] || die "could not determine the latest backup key"
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
LOCAL="${TMP}/restore.sql.gz"

log "downloading s3://${S3_BUCKET}/${KEY}"
aws s3 cp "s3://${S3_BUCKET}/${KEY}" "$LOCAL" --only-show-errors
gzip -t "$LOCAL" || die "downloaded backup failed its integrity check"

log "restoring into mysql-primary (this replaces the current data)"
# The GTID_PURGED value inside the dump conflicts with the server existing
# GTID state, so it is reset first. This is the documented way to load a
# GTID-aware dump.
docker exec -i mysql-primary mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" -e "RESET MASTER;" 2>/dev/null
zcat "$LOCAL" | docker exec -i mysql-primary mysql -uroot -p"${MYSQL_ROOT_PASSWORD}"

ROWS="$(docker exec -i mysql-primary mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" -N -B \
        -e "SELECT COUNT(*) FROM \`${MYSQL_DATABASE}\`.links;" 2>/dev/null || echo '?')"
log "restore complete - ${MYSQL_DATABASE}.links now has ${ROWS} rows"
warn "replication must be re-established after a restore: run scripts/db/setup_replication.sh"
