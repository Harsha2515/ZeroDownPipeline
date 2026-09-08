#!/usr/bin/env bash
# Start a new app container in the given colour slot.
#
# The container is published on 127.0.0.1 only. Nothing but nginx can reach the
# app ports, so the security group never has to open 8000/8001 to the internet.
# AWS credentials are NOT passed in - boto3 picks up the instance role from
# IMDS, which is why there are no long-lived keys anywhere on this box.
#
# Usage: start_container.sh <blue|green> <image_ref> <version> [break_healthcheck]

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

COLOR="${1:?usage: start_container.sh <blue|green> <image> <version> [break]}"
IMAGE="${2:?image reference required}"
VERSION="${3:?version (git short sha) required}"
BREAK="${4:-0}"

PORT="$(port_for_color "$COLOR")"
NAME="$(container_name "$COLOR")"

require_cmd docker
load_env "${ZDP_ROOT}/.env"
require_env MYSQL_DATABASE MYSQL_USER MYSQL_PASSWORD

log "pulling ${IMAGE}"
docker pull "$IMAGE"

# A stale container in this slot is expected on every second deploy - the slot
# is only ever the INACTIVE one, so removing it cannot affect live traffic.
if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
  log "removing stale container ${NAME}"
  docker rm -f "$NAME" >/dev/null
fi

docker network inspect zdp-net >/dev/null 2>&1 || docker network create zdp-net

log "starting ${NAME} from ${IMAGE} on 127.0.0.1:${PORT}"
docker run -d \
  --name "$NAME" \
  --network zdp-net \
  --restart unless-stopped \
  -p "127.0.0.1:${PORT}:${PORT}" \
  -e "APP_PORT=${PORT}" \
  -e "APP_COLOR=${COLOR}" \
  -e "APP_VERSION=${VERSION}" \
  -e "DB_HOST=${DB_HOST:-mysql-primary}" \
  -e "DB_PORT=${DB_PORT:-3306}" \
  -e "DB_REPLICA_HOST=${DB_REPLICA_HOST:-mysql-replica}" \
  -e "DB_REPLICA_PORT=${DB_REPLICA_PORT:-3306}" \
  -e "MYSQL_DATABASE=${MYSQL_DATABASE}" \
  -e "MYSQL_USER=${MYSQL_USER}" \
  -e "MYSQL_PASSWORD=${MYSQL_PASSWORD}" \
  -e "REDIS_HOST=${REDIS_HOST:-redis}" \
  -e "REDIS_PORT=${REDIS_PORT:-6379}" \
  -e "CACHE_TTL_SECONDS=${CACHE_TTL_SECONDS:-300}" \
  -e "S3_BUCKET=${S3_BUCKET:-}" \
  -e "AWS_REGION=${AWS_REGION:-ap-south-1}" \
  -e "BREAK_HEALTHCHECK=${BREAK}" \
  --memory 320m \
  --log-opt max-size=10m --log-opt max-file=3 \
  "$IMAGE" >/dev/null

log "container ${NAME} started (version=${VERSION}, break_healthcheck=${BREAK})"
