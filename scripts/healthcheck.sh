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
BODY_FILE="$(mktemp)"
trap 'rm -f "$BODY_FILE"' EXIT

while (( attempt <= RETRIES )); do
  # Capture the status code and the body separately. Using `curl -f` here would
  # discard the body on any HTTP error, making a container that answered 500
  # indistinguishable from one that never opened the port - and those two
  # failures need completely different fixes at 3am.
  # curl already writes 000 via -w when it cannot connect, so do NOT append a
  # fallback here - that produced "000000" and misreported a refused connection
  # as an unhealthy HTTP response.
  http_code="$(curl -sS --max-time 5 -o "$BODY_FILE" -w '%{http_code}' "$URL" 2>/dev/null || true)"
  [[ -z "$http_code" ]] && http_code="000"
  body="$(cat "$BODY_FILE" 2>/dev/null || true)"

  if [[ "$http_code" == "000" ]]; then
    warn "attempt ${attempt}/${RETRIES}: no response - nothing is listening on port ${PORT} yet"
  elif [[ "$http_code" != "200" ]]; then
    warn "attempt ${attempt}/${RETRIES}: the app answered HTTP ${http_code} (it is running but unhealthy): ${body}"
  else
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
      warn "attempt ${attempt}/${RETRIES}: HTTP 200 but status=${status}: ${body}"
    fi
  fi

  (( attempt++ ))
  (( attempt <= RETRIES )) && sleep "$INTERVAL"
done

err "health check FAILED after ${RETRIES} attempts against ${URL}"
err "container logs (last 40 lines):"
docker logs --tail 40 "$(container_name "$( [[ "$PORT" == "$BLUE_PORT" ]] && echo blue || echo green )")" >&2 2>/dev/null || true
exit 1
