#!/usr/bin/env bash
# Automated MySQL backup to S3.
#
# Three decisions worth defending in an interview:
#   1. The dump is taken from the REPLICA, not the primary. Backups are the
#      classic source of lock contention on a live database; the replica exists
#      partly so this cost lands somewhere that is not the write path.
#   2. --single-transaction gives a consistent InnoDB snapshot without locking
#      tables, and --set-gtid-purged=ON records the exact GTID position, so a
#      restored dump can be re-attached as a replica.
#   3. The dump is verified (non-empty, gzip-valid, contains a schema) BEFORE
#      it is uploaded. An unverified backup is not a backup.
#
# From cron:
#   0 2 * * * /opt/zerodown/scripts/db/backup_db.sh >> /var/log/zerodown-backup.log 2>&1

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib.sh"

require_cmd docker aws gzip
load_env "${ZDP_ROOT}/.env"
require_env MYSQL_ROOT_PASSWORD MYSQL_DATABASE S3_BUCKET

SOURCE_CONTAINER="${BACKUP_SOURCE:-mysql-replica}"
RETAIN_LOCAL="${RETAIN_LOCAL:-3}"
BACKUP_DIR="${ZDP_ROOT}/backups"
STAMP="$(date -u +'%Y%m%dT%H%M%SZ')"
FILE="${BACKUP_DIR}/${MYSQL_DATABASE}-${STAMP}.sql.gz"
S3_KEY="backups/mysql/$(date -u +'%Y/%m/%d')/${MYSQL_DATABASE}-${STAMP}.sql.gz"

mkdir -p "$BACKUP_DIR"

# Fall back to the primary if the replica is down - a missing replica must not
# mean a missing backup.
if ! docker ps --format '{{.Names}}' | grep -qx "$SOURCE_CONTAINER"; then
  warn "${SOURCE_CONTAINER} not running - falling back to mysql-primary"
  SOURCE_CONTAINER="mysql-primary"
fi

log "dumping ${MYSQL_DATABASE} from ${SOURCE_CONTAINER}"
if ! docker exec "$SOURCE_CONTAINER" mysqldump \
      -uroot -p"${MYSQL_ROOT_PASSWORD}" \
      --single-transaction \
      --routines --triggers --events \
      --set-gtid-purged=ON \
      --databases "${MYSQL_DATABASE}" 2>/dev/null | gzip -9 > "$FILE"; then
  rm -f "$FILE"
  die "mysqldump failed - no backup written"
fi

log "verifying dump"
[[ -s "$FILE" ]]                      || { rm -f "$FILE"; die "dump is empty"; }
gzip -t "$FILE"                       || { rm -f "$FILE"; die "dump failed its gzip integrity check"; }
zcat "$FILE" | grep -q 'CREATE TABLE' || { rm -f "$FILE"; die "dump contains no CREATE TABLE - refusing to upload"; }
SIZE="$(du -h "$FILE" | cut -f1)"
log "dump verified (${SIZE})"

log "uploading to s3://${S3_BUCKET}/${S3_KEY}"
aws s3 cp "$FILE" "s3://${S3_BUCKET}/${S3_KEY}" \
  --only-show-errors \
  --metadata "source=${SOURCE_CONTAINER},database=${MYSQL_DATABASE},taken_at=${STAMP}"

# A pointer to the newest backup, so restore_db.sh never has to guess.
printf '{"key":"%s","taken_at":"%s","source":"%s","size":"%s"}\n' \
  "$S3_KEY" "$STAMP" "$SOURCE_CONTAINER" "$SIZE" \
  | aws s3 cp - "s3://${S3_BUCKET}/backups/latest.json" \
      --content-type application/json --only-show-errors

log "pruning local backups, keeping the newest ${RETAIN_LOCAL}"
ls -1t "${BACKUP_DIR}"/*.sql.gz 2>/dev/null | tail -n +$((RETAIN_LOCAL + 1)) | xargs -r rm -f

log "backup complete: s3://${S3_BUCKET}/${S3_KEY}"
