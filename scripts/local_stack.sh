#!/usr/bin/env bash
# Run the whole stack on your own machine - no AWS, no cost.
#
# This exists because the expensive way to find a bug is on a remote instance
# over SSH. Everything except EC2, S3 and the nginx traffic switch behaves the
# same here as it does in production: the same image, the same MySQL pair with
# the same replication, the same Redis, the same health endpoint.
#
# Usage:
#   scripts/local_stack.sh up      build the image, start the data tier, run the app
#   scripts/local_stack.sh test    exercise the full request path and assert on it
#   scripts/local_stack.sh status  what is running
#   scripts/local_stack.sh logs    follow the app log
#   scripts/local_stack.sh down    stop everything and delete the local volumes
#
# Requires: Docker Desktop running, and a .env file (copy .env.example).

# ZDP_ROOT is /opt/zerodown on the instance; locally it is the repo itself.
ZDP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export ZDP_ROOT
. "${ZDP_ROOT}/scripts/lib.sh"

COMPOSE=("docker" "compose" "--env-file" "${ZDP_ROOT}/.env" "-f" "${ZDP_ROOT}/docker-compose.data.yml")
IMAGE="zdp-local:latest"
APP_NAME="zdp-app-blue"
APP_PORT_LOCAL="${APP_PORT_BLUE:-8000}"
BASE="http://127.0.0.1:${APP_PORT_LOCAL}"

require_cmd docker curl
[[ -f "${ZDP_ROOT}/.env" ]] || die "no .env - copy .env.example to .env first"

wait_healthy() {
  local name="$1" tries="${2:-60}" state=""
  for ((i = 1; i <= tries; i++)); do
    state="$(docker inspect -f '{{.State.Health.Status}}' "$name" 2>/dev/null || echo missing)"
    [[ "$state" == "healthy" ]] && { log "${name} healthy"; return 0; }
    sleep 5
  done
  docker logs "$name" 2>&1 | tail -20 >&2
  die "${name} never became healthy (last state: ${state})"
}

cmd_up() {
  log "building the application image"
  docker build --build-arg APP_VERSION=local -t "$IMAGE" "$ZDP_ROOT"

  docker network inspect zdp-net >/dev/null 2>&1 || docker network create zdp-net
  log "starting MySQL primary + replica and Redis"
  "${COMPOSE[@]}" up -d
  wait_healthy mysql-primary
  wait_healthy mysql-replica

  "${ZDP_ROOT}/scripts/db/setup_replication.sh"

  load_env "${ZDP_ROOT}/.env"
  log "starting the app container"
  docker rm -f "$APP_NAME" >/dev/null 2>&1 || true
  docker run -d --name "$APP_NAME" \
    --network zdp-net \
    -p "127.0.0.1:${APP_PORT_LOCAL}:${APP_PORT_LOCAL}" \
    -e "APP_PORT=${APP_PORT_LOCAL}" \
    -e "APP_COLOR=blue" \
    -e "APP_VERSION=local" \
    -e "DB_HOST=mysql-primary" \
    -e "DB_REPLICA_HOST=mysql-replica" \
    -e "MYSQL_DATABASE=${MYSQL_DATABASE}" \
    -e "MYSQL_USER=${MYSQL_USER}" \
    -e "MYSQL_PASSWORD=${MYSQL_PASSWORD}" \
    -e "REDIS_HOST=redis" \
    -e "S3_BUCKET=" \
    "$IMAGE" >/dev/null

  log "waiting for the app to report healthy"
  for _ in $(seq 1 20); do
    if curl -fsS --max-time 3 "${BASE}/health" >/dev/null 2>&1; then
      log "stack is up: ${BASE}"
      curl -s "${BASE}/health"; echo
      return 0
    fi
    sleep 3
  done
  docker logs "$APP_NAME" 2>&1 | tail -20 >&2
  die "the app never became healthy"
}

# Assertions, not just output to eyeball. Every check either passes loudly or
# fails the script, so this can be trusted without reading it closely.
cmd_test() {
  local failures=0
  check() {
    local label="$1" expected="$2" actual="$3"
    if [[ "$actual" == "$expected" ]]; then
      printf '  PASS  %-46s %s\n' "$label" "$actual"
    else
      printf '  FAIL  %-46s got %s, want %s\n' "$label" "$actual" "$expected"
      failures=$((failures + 1))
    fi
  }

  echo
  echo "  Running end-to-end checks against ${BASE}"
  echo "  ------------------------------------------------------------------"

  check "health returns 200" "200" \
    "$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/health")"

  local health; health="$(curl -s "${BASE}/health")"
  check "mysql primary reachable" "true" \
    "$(printf '%s' "$health" | grep -oE '"mysql_primary":[a-z]+' | cut -d: -f2)"
  check "mysql replica reachable" "true" \
    "$(printf '%s' "$health" | grep -oE '"mysql_replica":[a-z]+' | cut -d: -f2)"
  check "redis reachable" "true" \
    "$(printf '%s' "$health" | grep -oE '"redis":[a-z]+' | cut -d: -f2)"

  local created code
  created="$(curl -s -X POST "${BASE}/shorten" -H 'Content-Type: application/json' \
             -d '{"url":"https://example.com/local-test"}')"
  code="$(printf '%s' "$created" | grep -oE '"short_code":"[^"]+"' | cut -d'"' -f4)"
  check "shorten returns a 7-character code" "7" "${#code}"

  check "redirect is a 302" "302" \
    "$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/${code}")"
  check "redirect target is correct" "https://example.com/local-test" \
    "$(curl -s -o /dev/null -w '%header{Location}' "${BASE}/${code}")"

  # /stats reads the replica, so this passing means replication works. Each of
  # the two redirect checks above issued a real request, hence 2 clicks - do
  # not add another curl here without changing this number.
  sleep 1
  check "click count via the REPLICA" "2" \
    "$(curl -s "${BASE}/stats/${code}" | grep -oE '"clicks":[0-9]+' | cut -d: -f2)"

  check "invalid url rejected" "400" \
    "$(curl -s -o /dev/null -w '%{http_code}' -X POST "${BASE}/shorten" \
       -H 'Content-Type: application/json' -d '{"url":"javascript:alert(1)"}')"
  check "unknown code is 404" "404" \
    "$(curl -s -o /dev/null -w '%{http_code}' "${BASE}/nosuchcode")"

  load_env "${ZDP_ROOT}/.env"
  check "row present on the primary" "1" \
    "$(docker exec -i mysql-primary mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" -N -B \
       -e "SELECT COUNT(*) FROM ${MYSQL_DATABASE}.links WHERE short_code='${code}';" 2>/dev/null)"
  check "row replicated to the replica" "1" \
    "$(docker exec -i mysql-replica mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" -N -B \
       -e "SELECT COUNT(*) FROM ${MYSQL_DATABASE}.links WHERE short_code='${code}';" 2>/dev/null)"
  check "replica is read-only" "1" \
    "$(docker exec -i mysql-replica mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" -N -B \
       -e "SELECT @@super_read_only;" 2>/dev/null)"

  echo "  ------------------------------------------------------------------"
  if (( failures == 0 )); then
    log "all checks passed"
    return 0
  fi
  die "${failures} check(s) failed"
}

cmd_status() {
  docker ps --filter network=zdp-net --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
  echo
  "${ZDP_ROOT}/scripts/db/replication_status.sh" || true
}

cmd_logs() { docker logs -f "$APP_NAME"; }

cmd_down() {
  log "removing the app container"
  docker rm -f "$APP_NAME" >/dev/null 2>&1 || true
  log "stopping the data tier and deleting its volumes"
  "${COMPOSE[@]}" down -v
  log "local stack removed"
}

case "${1:-}" in
  up)     cmd_up ;;
  test)   cmd_test ;;
  status) cmd_status ;;
  logs)   cmd_logs ;;
  down)   cmd_down ;;
  *) die "usage: local_stack.sh {up|test|status|logs|down}" ;;
esac
