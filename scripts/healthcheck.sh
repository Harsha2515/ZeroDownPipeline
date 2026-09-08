#!/usr/bin/env bash
# Poll a container's /health until it passes or the retry budget runs out.
#
# This is the gate the whole pipeline turns on, so it is deliberately strict:
#   - only HTTP 200 with {"status":"ok"|"degraded"} counts as healthy
#   - the reported version must match the version we just deployed, otherwise
#     we are looking at the OLD container on a recycled port and would promote
#     a build that never actually started
#
# Usage: healthcheck.sh <port> <expected_version> [retries] [interval_seconds]
# Exit:  0 healthy, 1 unhealthy

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

PORT="${1:?usage: healthcheck.sh <port> <expected_version> [retries] [interval]}"
EXPECTED_VERSION="${2:?expected version (git short sha) required}"
RETRIES="${3:-10}"
INTERVAL="${4:-3}"
URL="http://127.0.0.1:${PORT}/health"

require_cmd curl

log "health-checking ${URL} for version=${EXPECTED_VERSION} (${RETRIES} attempts, ${INTERVAL}s apart)"

attempt=1
while (( attempt <= RETRIES )); do
  body="$(curl -fsS --max-time 5 "$URL" 2>/dev/null || true)"

  if [[ -n "$body" ]]; then
    status="$(printf '%s' "$body" | grep -oE '"status"[[:space:]]*:[[:space:]]*"[^"]+"' | cut -d'"' -f4 || true)"
    version="$(printf '%s' "$body" | grep -oE '"version"[[:space:]]*:[[:space:]]*"[^"]+"' | cut -d'"' -f4 || true)"

    if [[ "$status" == "ok" || "$status" == "degraded" ]]; then
      if [[ "$version" == "$EXPECTED_VERSION" ]]; then
        log "attempt ${attempt}/${RETRIES}: healthy (status=${status} version=${version})"
        [[ "$status" == "degraded" ]] && warn "app is DEGRADED - a non-critical dependency is down: ${body}"
        exit 0
      fi
      warn "attempt ${attempt}/${RETRIES}: healthy but version mismatch (got '${version}', want '${EXPECTED_VERSION}')"
    else
      warn "attempt ${attempt}/${RETRIES}: unhealthy response: ${body}"
    fi
  else
    warn "attempt ${attempt}/${RETRIES}: no response from ${URL}"
  fi

  (( attempt++ ))
  (( attempt <= RETRIES )) && sleep "$INTERVAL"
done

err "health check FAILED after ${RETRIES} attempts against ${URL}"
err "container logs (last 40 lines):"
docker logs --tail 40 "$(container_name "$( [[ "$PORT" == "$BLUE_PORT" ]] && echo blue || echo green )")" >&2 2>/dev/null || true
exit 1
