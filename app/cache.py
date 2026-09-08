"""Redis cache for the redirect hot path.

Every function here is best-effort: if Redis is down the app still serves
correct responses straight from MySQL. A cache outage must not become an
application outage, and the health check reports Redis separately so the
pipeline can distinguish "degraded" from "broken".
"""
import logging

import redis

from . import config

log = logging.getLogger(__name__)

_client = redis.Redis(
    host=config.REDIS_HOST,
    port=config.REDIS_PORT,
    socket_connect_timeout=2,
    socket_timeout=2,
    decode_responses=True,
)


def ping() -> bool:
    try:
        return bool(_client.ping())
    except Exception as exc:  # noqa: BLE001
        log.warning("redis ping failed: %s", exc)
        return False


def get_url(short_code: str):
    try:
        return _client.get(f"link:{short_code}")
    except Exception as exc:  # noqa: BLE001
        log.warning("redis get failed: %s", exc)
        return None


def set_url(short_code: str, url: str) -> None:
    try:
        _client.setex(f"link:{short_code}", config.CACHE_TTL_SECONDS, url)
    except Exception as exc:  # noqa: BLE001
        log.warning("redis set failed: %s", exc)


def invalidate(short_code: str) -> None:
    try:
        _client.delete(f"link:{short_code}")
    except Exception as exc:  # noqa: BLE001
        log.warning("redis delete failed: %s", exc)
