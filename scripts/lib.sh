#!/usr/bin/env bash
# Shared helpers for every script in this repo. Source it, don't execute it.
#
#   . "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

# Fail loudly: any unset variable, any failed command, any failed pipe stage.
set -euo pipefail

ZDP_ROOT="${ZDP_ROOT:-/opt/zerodown}"
ZDP_STATE_DIR="${ZDP_STATE_DIR:-${ZDP_ROOT}/state}"
NGINX_CONF="${NGINX_CONF:-/etc/nginx/conf.d/zerodown.conf}"

_ts() { date -u +'%Y-%m-%dT%H:%M:%SZ'; }

log()  { printf '%s [ INFO] %s\n' "$(_ts)" "$*" >&2; }
warn() { printf '%s [ WARN] %s\n' "$(_ts)" "$*" >&2; }
err()  { printf '%s [ERROR] %s\n' "$(_ts)" "$*" >&2; }
die()  { err "$*"; exit 1; }

# Load KEY=VALUE pairs from a .env file without executing arbitrary shell.
load_env() {
  local file="${1:-${ZDP_ROOT}/.env}"
  [[ -f "$file" ]] || { warn "no env file at $file"; return 0; }
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    # A .env authored on Windows arrives with CRLF endings. Without this, every
    # value silently carries a trailing  - passwords stop matching, ports
    # stop parsing, and the errors point everywhere except the real cause.
    line="${line%$''}"
    [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue
    [[ "$line" != *=* ]] && continue
    key="${line%%=*}"
    value="${line#*=}"
    key="$(printf '%s' "$key" | tr -d '[:space:]')"
    # Strip one layer of surrounding quotes if present.
    value="${value%\"}"; value="${value#\"}"
    value="${value%\'}"; value="${value#\'}"
    export "${key}=${value}"
  done < "$file"
}

require_env() {
  local missing=()
  for name in "$@"; do
    [[ -n "${!name:-}" ]] || missing+=("$name")
  done
  (( ${#missing[@]} == 0 )) || die "missing required env: ${missing[*]}"
}

# Ports come from .env when it is present, so changing them there is enough -
# nothing in this repo hardcodes 8000/8001 anywhere else.
load_env "${ZDP_ROOT}/.env" 2>/dev/null || true
BLUE_PORT="${APP_PORT_BLUE:-8000}"
GREEN_PORT="${APP_PORT_GREEN:-8001}"

require_cmd() {
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || die "required command not found: $c"
  done
}

port_for_color() {
  case "$1" in
    blue)  printf '%s' "$BLUE_PORT" ;;
    green) printf '%s' "$GREEN_PORT" ;;
    *) die "unknown color: $1" ;;
  esac
}

other_color() {
  case "$1" in
    blue)  printf 'green' ;;
    green) printf 'blue' ;;
    *) die "unknown color: $1" ;;
  esac
}

# The live color is whatever nginx is currently proxying to. Nginx config is
# the single source of truth here on purpose: a state file can drift from
# reality, the thing actually routing traffic cannot.
active_color() {
  if [[ -f "$NGINX_CONF" ]]; then
    local port
    port="$(grep -oE 'server[[:space:]]+127\.0\.0\.1:[0-9]+' "$NGINX_CONF" | grep -oE '[0-9]+$' | head -1 || true)"
    case "$port" in
      "$BLUE_PORT")  printf 'blue';  return 0 ;;
      "$GREEN_PORT") printf 'green'; return 0 ;;
    esac
  fi
  # Nothing deployed yet - treat blue as the slot to fill first.
  printf 'none'
}

# The slot the next deploy goes into: the one not currently serving traffic.
inactive_color() {
  local active; active="$(active_color)"
  if [[ "$active" == "none" ]]; then printf 'blue'; else other_color "$active"; fi
}

container_name() { printf 'zdp-app-%s' "$1"; }
