#!/usr/bin/env bash
# Human-readable replication health. Exits non-zero if replication is broken,
# so it can be used as a check in a cron job or a monitoring hook.

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib.sh"

load_env "${ZDP_ROOT}/.env"
require_env MYSQL_ROOT_PASSWORD

status="$(docker exec -i mysql-replica mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" -e "SHOW REPLICA STATUS\G" 2>/dev/null || true)"
[[ -n "$status" ]] || die "could not read replica status - is mysql-replica running?"

io="$(printf '%s' "$status"  | grep -E '^\s*Replica_IO_Running:'  | awk '{print $2}')"
sql="$(printf '%s' "$status" | grep -E '^\s*Replica_SQL_Running:' | awk '{print $2}')"
lag="$(printf '%s' "$status" | grep -E '^\s*Seconds_Behind_Source:' | awk '{print $2}')"
err_msg="$(printf '%s' "$status" | grep -E '^\s*Last_Error:' | cut -d: -f2- | sed 's/^ *//')"

printf '  IO thread            : %s\n'  "${io:-?}"
printf '  SQL thread           : %s\n'  "${sql:-?}"
printf '  Seconds behind source: %s\n'  "${lag:-?}"
[[ -n "$err_msg" ]] && printf '  Last error           : %s\n' "$err_msg"

if [[ "$io" == "Yes" && "$sql" == "Yes" ]]; then
  log "replication healthy"
  exit 0
fi
err "replication is NOT healthy"
exit 1
