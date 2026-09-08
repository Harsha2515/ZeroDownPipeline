#!/usr/bin/env bash
# Bring up the stateful tier (MySQL primary + replica, Redis) and wire up
# replication. Idempotent - safe to re-run; it will not clobber existing data.
#
# Run this ONCE after provisioning the instance. The deploy pipeline never
# calls it: application deploys must not be able to disturb the database.

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib.sh"

require_cmd docker
load_env "${ZDP_ROOT}/.env"
require_env MYSQL_ROOT_PASSWORD MYSQL_DATABASE MYSQL_USER MYSQL_PASSWORD MYSQL_REPL_USER MYSQL_REPL_PASSWORD

COMPOSE_FILE="${ZDP_ROOT}/docker-compose.data.yml"
[[ -f "$COMPOSE_FILE" ]] || die "compose file not found at ${COMPOSE_FILE}"

log "ensuring docker network zdp-net exists"
docker network inspect zdp-net >/dev/null 2>&1 || docker network create zdp-net

log "starting data tier"
docker compose --env-file "${ZDP_ROOT}/.env" -f "$COMPOSE_FILE" up -d

wait_healthy() {
  local name="$1" tries="${2:-60}"
  log "waiting for ${name} to become healthy"
  for ((i = 1; i <= tries; i++)); do
    local state
    state="$(docker inspect -f '{{.State.Health.Status}}' "$name" 2>/dev/null || echo missing)"
    [[ "$state" == "healthy" ]] && { log "${name} is healthy"; return 0; }
    sleep 5
  done
  die "${name} did not become healthy in time (last state: ${state:-unknown})"
}

wait_healthy mysql-primary
wait_healthy mysql-replica

"$(dirname "${BASH_SOURCE[0]}")/setup_replication.sh"

log "data tier is up. Run scripts/db/replication_status.sh to verify."
