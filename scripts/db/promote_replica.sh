#!/usr/bin/env bash
# Fail over: promote the replica to primary and repoint the application at it.
#
# The sequence matters.
#   1. Let the replica finish applying what it has already received, so the
#      promotion loses as little as possible.
#   2. Stop replication and clear the replication config - a server that still
#      thinks it has a source is not a primary.
#   3. Only then drop read_only. Doing it earlier would allow writes to a
#      server that is still applying the old primary binlog.
#   4. Rewrite DB_HOST and restart the live container, so the application
#      actually uses the new primary. A failover that no traffic follows is
#      not a failover.
#
# Usage: promote_replica.sh --confirm

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib.sh"

require_cmd docker
load_env "${ZDP_ROOT}/.env"
require_env MYSQL_ROOT_PASSWORD MYSQL_DATABASE

[[ "${1:-}" == "--confirm" ]] || die "promote_replica.sh is a failover action - re-run with --confirm"

replica_sql() { docker exec -i mysql-replica mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" -N -B -e "$1"; }
# Without column names, \G output cannot be grepped for a field - see the same
# helper in setup_replication.sh.
replica_status() { docker exec -i mysql-replica mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" -e "SHOW REPLICA STATUS\G"; }

START_TS="$(date +%s)"
DRAINED_STATE="Replica has read all relay log"

log "step 1/4: draining the replica relay log"
replica_sql "SELECT 1;" >/dev/null || die "mysql-replica is not reachable - cannot promote"
# Best effort: if the old primary is already gone this returns immediately.
replica_sql "STOP REPLICA IO_THREAD;" || true
for _ in $(seq 1 30); do
  if replica_status | grep -q "$DRAINED_STATE"; then
    log "relay log drained"
    break
  fi
  sleep 1
done

log "step 2/4: stopping replication and clearing replication config"
replica_sql "STOP REPLICA; RESET REPLICA ALL;"

log "step 3/4: making the replica writable"
# PERSIST, to match how read_only was armed. SET GLOBAL alone would leave the
# newly promoted primary read-only again after its next restart - a failover
# that quietly undoes itself is worse than one that never happened.
replica_sql "SET PERSIST super_read_only = OFF; SET PERSIST read_only = OFF;"

log "step 4/4: repointing the application at mysql-replica"
ENV_FILE="${ZDP_ROOT}/.env"
if grep -q '^DB_HOST=' "$ENV_FILE"; then
  sed -i 's/^DB_HOST=.*/DB_HOST=mysql-replica/' "$ENV_FILE"
else
  printf '\nDB_HOST=mysql-replica\n' >> "$ENV_FILE"
fi
# Reads have nowhere else to go now; point them at the new primary too.
if grep -q '^DB_REPLICA_HOST=' "$ENV_FILE"; then
  sed -i 's/^DB_REPLICA_HOST=.*/DB_REPLICA_HOST=mysql-replica/' "$ENV_FILE"
fi

ACTIVE="$(active_color)"
if [[ "$ACTIVE" != "none" ]]; then
  NAME="$(container_name "$ACTIVE")"
  IMAGE="$(docker inspect -f '{{.Config.Image}}' "$NAME" 2>/dev/null || true)"
  VERSION="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$NAME" 2>/dev/null \
             | grep '^APP_VERSION=' | cut -d= -f2 || echo unknown)"
  if [[ -n "$IMAGE" ]]; then
    log "restarting the live ${ACTIVE} container against the new primary"
    "${ZDP_ROOT}/scripts/start_container.sh" "$ACTIVE" "$IMAGE" "$VERSION"
  else
    warn "could not determine the live image - restart the app container by hand"
  fi
else
  warn "no active colour found - start an app container to complete the failover"
fi

ELAPSED=$(( $(date +%s) - START_TS ))
log "failover complete in ${ELAPSED}s - mysql-replica is now the primary"
warn "do NOT restart the old primary as a writer. Rebuild it as the new replica before reattaching."
