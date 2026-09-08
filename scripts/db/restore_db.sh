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

# Restoring only the primary silently diverges the pair. The replica still holds
# every transaction that happened after the backup was taken, and because those
# GTIDs are already in its executed set, replication reconnects, reports
# healthy, and never reconciles the difference - leaving a replica that would
# promote a database containing rows the primary does not have.
#
# So the same dump is loaded into the replica too, from the same GTID position.
if docker ps --format '{{.Names}}' | grep -qx mysql-replica; then
  log "resetting the replica and loading the same dump into it"
  docker exec -i mysql-replica mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" -e "
    STOP REPLICA;
    RESET REPLICA ALL;
    SET PERSIST super_read_only = OFF;
    SET PERSIST read_only = OFF;
    RESET MASTER;
  " 2>/dev/null
  zcat "$LOCAL" | docker exec -i mysql-replica mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" 2>/dev/null
else
  warn "mysql-replica is not running - rebuild it before trusting a failover"
fi

rows_in() {
  docker exec -i "$1" mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" -N -B \
    -e "SELECT COUNT(*) FROM \`${MYSQL_DATABASE}\`.links;" 2>/dev/null || echo '?'
}
PRIMARY_ROWS="$(rows_in mysql-primary)"
REPLICA_ROWS="$(rows_in mysql-replica)"

log "restore complete - ${MYSQL_DATABASE}.links: primary=${PRIMARY_ROWS} replica=${REPLICA_ROWS}"
if [[ "$PRIMARY_ROWS" != "$REPLICA_ROWS" ]]; then
  err "primary and replica disagree after the restore - do NOT fail over until resolved"
fi
warn "re-establish replication now: scripts/db/setup_replication.sh --force"
