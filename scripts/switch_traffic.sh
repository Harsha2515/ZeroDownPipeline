#!/usr/bin/env bash
# Point nginx at a colour and reload it gracefully.
#
# `nginx -s reload` is what makes this zero-downtime: the master process starts
# new workers on the new config and lets the old workers finish the requests
# they are already holding before they exit. No connection is ever dropped.
#
# The config is validated with `nginx -t` BEFORE the reload, and the previous
# config is kept so a bad render can be put back instead of leaving the box
# with no working proxy.
#
# Usage: switch_traffic.sh <blue|green>

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"

TARGET_COLOR="${1:?usage: switch_traffic.sh <blue|green>}"
TARGET_PORT="$(port_for_color "$TARGET_COLOR")"
TEMPLATE="${ZDP_ROOT}/deploy/nginx/app.conf.template"

require_cmd nginx
[[ -f "$TEMPLATE" ]] || die "nginx template not found at ${TEMPLATE}"

PREVIOUS_COLOR="$(active_color)"
log "switching traffic: ${PREVIOUS_COLOR} -> ${TARGET_COLOR} (port ${TARGET_PORT})"

# The previous config is kept OUTSIDE /etc/nginx/conf.d. A backup sitting in a
# directory nginx globs is an accident waiting for someone to rename it.
BACKUP=""
if [[ -f "$NGINX_CONF" ]]; then
  sudo mkdir -p "$ZDP_STATE_DIR"
  BACKUP="${ZDP_STATE_DIR}/nginx-zerodown.conf.prev"
  sudo cp "$NGINX_CONF" "$BACKUP"
fi

TMP="$(mktemp)"
sed -e "s/{{ACTIVE_PORT}}/${TARGET_PORT}/g" \
    -e "s/{{ACTIVE_COLOR}}/${TARGET_COLOR}/g" \
    "$TEMPLATE" > "$TMP"
sudo install -m 0644 "$TMP" "$NGINX_CONF"
rm -f "$TMP"

if ! sudo nginx -t; then
  err "rendered nginx config is invalid - restoring previous config"
  if [[ -n "$BACKUP" ]]; then
    sudo cp "$BACKUP" "$NGINX_CONF"
    sudo nginx -t && sudo nginx -s reload
  else
    sudo rm -f "$NGINX_CONF"
  fi
  die "nginx config validation failed, traffic unchanged (still ${PREVIOUS_COLOR})"
fi

# systemctl reload is `nginx -s reload` under the hood, and handles the case
# where nginx is not running yet.
if systemctl is-active --quiet nginx; then
  sudo systemctl reload nginx
else
  sudo systemctl start nginx
fi

sudo mkdir -p "$ZDP_STATE_DIR"
printf '%s' "$TARGET_COLOR" | sudo tee "${ZDP_STATE_DIR}/active_color" >/dev/null

log "traffic now served by ${TARGET_COLOR} on port ${TARGET_PORT}"
