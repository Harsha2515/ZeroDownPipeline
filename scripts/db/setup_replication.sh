#!/usr/bin/env bash
# Configure GTID-based replication from mysql-primary to mysql-replica.
#
# GTID auto-positioning is used instead of file/position coordinates: the
# replica asks the primary for "every transaction I have not seen", so this
# script is safe to re-run and a promoted replica can be re-attached later
# without anyone reading binlog offsets off a screen.
#
# Idempotent: if the replica is already running, it is left alone.

. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/../lib.sh"

load_env "${ZDP_ROOT}/.env"
require_env MYSQL_ROOT_PASSWORD MYSQL_DATABASE MYSQL_USER MYSQL_PASSWORD MYSQL_REPL_USER MYSQL_REPL_PASSWORD

primary_sql() { docker exec -i mysql-primary mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" -N -B -e "$1"; }
replica_sql() { docker exec -i mysql-replica mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" -N -B -e "$1"; }

# SHOW REPLICA STATUS needs its own helper. The -N above is --skip-column-names,
# which strips the field labels out of \G vertical output - so grepping the
# result for "Replica_SQL_Running: Yes" silently never matches, and healthy
# replication gets reported as broken.
replica_status() { docker exec -i mysql-replica mysql -uroot -p"${MYSQL_ROOT_PASSWORD}" -e "SHOW REPLICA STATUS\G"; }

if [[ "$(replica_status | grep -c 'Replica_IO_Running: Yes' || true)" -gt 0 ]]; then
  log "replication already running - nothing to do"
  exit 0
fi

log "creating replication user '${MYSQL_REPL_USER}' on the primary"
primary_sql "
  CREATE USER IF NOT EXISTS '${MYSQL_REPL_USER}'@'%' IDENTIFIED WITH mysql_native_password BY '${MYSQL_REPL_PASSWORD}';
  ALTER USER '${MYSQL_REPL_USER}'@'%' IDENTIFIED WITH mysql_native_password BY '${MYSQL_REPL_PASSWORD}';
  GRANT REPLICATION SLAVE ON *.* TO '${MYSQL_REPL_USER}'@'%';
  FLUSH PRIVILEGES;
"

# Note what is deliberately NOT done here: the application database and user
# are not created on the replica by hand. The primary binlogs its own
# initialisation, so GTID replication delivers both. Creating them locally
# first makes the replica reject the primary's CREATE USER with
# "Operation CREATE USER failed" and stops the SQL thread dead. The check
# after START REPLICA below confirms they arrived instead.

log "pointing the replica at the primary (GTID auto-position)"
replica_sql "
  STOP REPLICA;
  RESET REPLICA ALL;
  CHANGE REPLICATION SOURCE TO
    SOURCE_HOST     = 'mysql-primary',
    SOURCE_PORT     = 3306,
    SOURCE_USER     = '${MYSQL_REPL_USER}',
    SOURCE_PASSWORD = '${MYSQL_REPL_PASSWORD}',
    SOURCE_AUTO_POSITION = 1,
    GET_SOURCE_PUBLIC_KEY = 1;
  START REPLICA;
"

# SET PERSIST, not SET GLOBAL: this writes to mysqld-auto.cnf, so the replica
# comes back read-only after a restart. With SET GLOBAL a container restart
# would quietly produce a second writable server - the setup for a split brain.
log "arming read-only protection on the replica (persisted across restarts)"
replica_sql "SET PERSIST read_only = ON; SET PERSIST super_read_only = ON;"

log "waiting for the replica to apply the primary's backlog"
for _ in $(seq 1 30); do
  if replica_status | grep -q 'Replica_SQL_Running: Yes'; then
    break
  fi
  sleep 2
done

if ! replica_status | grep -q 'Replica_SQL_Running: Yes'; then
  replica_status | grep -E 'Running|Last_Error' >&2 || true
  die "replication did not start - see status above"
fi

# A failover is worthless if the promoted server has no application user, so
# confirm replication actually carried it across rather than assuming it did.
log "verifying the application user and database reached the replica"
for _ in $(seq 1 30); do
  have_user="$(replica_sql "SELECT COUNT(*) FROM mysql.user WHERE user = '${MYSQL_USER}';" 2>/dev/null || echo 0)"
  have_db="$(replica_sql "SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name = '${MYSQL_DATABASE}';" 2>/dev/null || echo 0)"
  if [[ "$have_user" -ge 1 && "$have_db" -ge 1 ]]; then
    log "replication is running, and ${MYSQL_USER}/${MYSQL_DATABASE} are present on the replica"
    exit 0
  fi
  sleep 2
done

warn "replication is running but ${MYSQL_USER} or ${MYSQL_DATABASE} has not appeared on the replica yet"
warn "check 'Seconds_Behind_Source' - a large backlog is normal on a first sync"
