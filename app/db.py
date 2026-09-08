"""MySQL access layer.

Two connection targets on purpose:
  * primary  - all writes, and reads that must be read-after-write consistent
  * replica  - /stats reads, so the demo actually exercises the replica

Connections are opened per request rather than pooled. At this project's scale
that is cheaper than the failure modes a hand-rolled pool introduces, and it
means a primary failover is picked up on the very next request with no restart.
"""
import contextlib
import logging

import pymysql

from . import config

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS links (
    short_code   VARCHAR(16)  NOT NULL PRIMARY KEY,
    original_url TEXT         NOT NULL,
    clicks       BIGINT       NOT NULL DEFAULT 0,
    created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def _connect(host: str, port: int):
    return pymysql.connect(
        host=host,
        port=port,
        user=config.DB_USER,
        password=config.DB_PASSWORD,
        database=config.DB_NAME,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        connect_timeout=3,
        read_timeout=5,
        write_timeout=5,
    )


@contextlib.contextmanager
def primary():
    conn = _connect(config.DB_HOST, config.DB_PORT)
    try:
        yield conn
    finally:
        conn.close()


@contextlib.contextmanager
def replica():
    """Read connection. Falls back to the primary if the replica is unreachable,
    so losing the replica degrades performance but never availability."""
    try:
        conn = _connect(config.DB_REPLICA_HOST, config.DB_REPLICA_PORT)
    except pymysql.MySQLError as exc:
        log.warning("replica unreachable (%s), falling back to primary", exc)
        conn = _connect(config.DB_HOST, config.DB_PORT)
    try:
        yield conn
    finally:
        conn.close()


def init_schema() -> None:
    with primary() as conn, conn.cursor() as cur:
        cur.execute(SCHEMA)


def ping(which: str = "primary") -> bool:
    ctx = primary if which == "primary" else replica
    try:
        with ctx() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()
        return True
    except Exception as exc:  # noqa: BLE001 - health check must never raise
        log.warning("db ping failed (%s): %s", which, exc)
        return False


def insert_link(short_code: str, original_url: str) -> None:
    with primary() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO links (short_code, original_url) VALUES (%s, %s)",
            (short_code, original_url),
        )


def get_link(short_code: str, use_replica: bool = False):
    ctx = replica if use_replica else primary
    with ctx() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT short_code, original_url, clicks, created_at "
            "FROM links WHERE short_code = %s",
            (short_code,),
        )
        return cur.fetchone()


def increment_clicks(short_code: str) -> None:
    with primary() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE links SET clicks = clicks + 1 WHERE short_code = %s",
            (short_code,),
        )
