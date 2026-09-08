#!/usr/bin/env bash
# Stop and remove the container in a colour slot.
#
# The grace period matters on the success path: after nginx has switched away
# from the old container, requests already in flight on the old workers still
# need to finish. `docker stop -t` sends SIGTERM and waits, which is what makes
# the promotion drop zero connections.
#
# Usage: stop_container.sh <blue|green> [grace_seconds]

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

COLOR="${1:?usage: stop_container.sh <blue|green> [grace_seconds]}"
GRACE="${2:-15}"
NAME="$(container_name "$COLOR")"

require_cmd docker

if ! docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  log "no container named ${NAME} - nothing to stop"
  exit 0
fi

log "stopping ${NAME} with a ${GRACE}s grace period"
docker stop -t "$GRACE" "$NAME" >/dev/null || warn "docker stop returned non-zero for ${NAME}"
docker rm "$NAME" >/dev/null || warn "docker rm returned non-zero for ${NAME}"
log "removed ${NAME}"
