"""URL shortener API.

Deliberately small: the interesting engineering in this project is the pipeline
around it, not the business logic. What this app owes the pipeline is an honest
/health endpoint - one that reports its own version and its real dependencies,
so a deploy can be promoted or rolled back on evidence rather than on hope.
"""
import logging
import os
import secrets
import string
from urllib.parse import urlparse

from flask import Flask, jsonify, redirect, request

from . import cache, config, db, s3_store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("zerodown")

app = Flask(__name__)

ALPHABET = string.ascii_letters + string.digits
CODE_LENGTH = 7


def _new_code() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(CODE_LENGTH))


def _valid_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def _base_url() -> str:
    return (config.BASE_URL or request.host_url).rstrip("/")


@app.post("/shorten")
def shorten():
    payload = request.get_json(silent=True) or {}
    url = (payload.get("url") or "").strip()
    if not _valid_url(url):
        return jsonify({"error": "a valid http(s) 'url' is required"}), 400

    # Collisions are astronomically unlikely at 62^7, but a duplicate primary
    # key must never surface to the caller as a 500.
    for _ in range(5):
        code = _new_code()
        try:
            db.insert_link(code, url)
            break
        except Exception as exc:  # noqa: BLE001
            log.warning("insert failed for code %s: %s", code, exc)
    else:
        return jsonify({"error": "could not allocate a short code"}), 503

    cache.set_url(code, url)
    s3_store.put_record(code, url)
    return jsonify({
        "short_code": code,
        "short_url": f"{_base_url()}/{code}",
        "original_url": url,
    }), 201


@app.get("/<short_code>")
def follow(short_code: str):
    url = cache.get_url(short_code)
    if url is None:
        row = db.get_link(short_code)
        if not row:
            return jsonify({"error": "not found"}), 404
        url = row["original_url"]
        cache.set_url(short_code, url)

    db.increment_clicks(short_code)
    return redirect(url, code=302)


@app.get("/stats/<short_code>")
def stats(short_code: str):
    # Read from the replica on purpose - this is the read path that proves
    # replication is actually working end to end.
    row = db.get_link(short_code, use_replica=True)
    if not row:
        return jsonify({"error": "not found"}), 404
    return jsonify({
        "short_code": row["short_code"],
        "original_url": row["original_url"],
        "clicks": row["clicks"],
        "created_at": row["created_at"].isoformat() if row["created_at"] else None,
    })


@app.get("/health")
def health():
    """The gate the pipeline deploys against.

    Non-200 means "do not promote this container". The primary being reachable
    is the only hard requirement; Redis and the replica are reported but only
    downgrade the status to 'degraded', because the app serves correct
    responses without them.
    """
    # Escape hatch used by the rollback demo: set BREAK_HEALTHCHECK=1 on a
    # deploy to simulate a bad release without shipping actually broken code.
    if os.getenv("BREAK_HEALTHCHECK", "").lower() in ("1", "true", "yes"):
        return jsonify({
            "status": "error",
            "version": config.APP_VERSION,
            "color": config.APP_COLOR,
            "reason": "BREAK_HEALTHCHECK is set",
        }), 500

    checks = {
        "mysql_primary": db.ping("primary"),
        "mysql_replica": db.ping("replica"),
        "redis": cache.ping(),
    }
    if not checks["mysql_primary"]:
        status, code = "error", 503
    elif not all(checks.values()):
        status, code = "degraded", 200
    else:
        status, code = "ok", 200

    return jsonify({
        "status": status,
        "version": config.APP_VERSION,
        "color": config.APP_COLOR,
        "checks": checks,
    }), code


@app.get("/")
def index():
    return jsonify({
        "service": "zerodownpipeline-api",
        "version": config.APP_VERSION,
        "color": config.APP_COLOR,
        "endpoints": ["POST /shorten", "GET /<code>", "GET /stats/<code>", "GET /health"],
    })


def create_app():
    """Entry point for gunicorn. Schema creation is idempotent and tolerant of
    a database that is not up yet - the health check is what gates the deploy."""
    try:
        db.init_schema()
    except Exception as exc:  # noqa: BLE001
        log.warning("schema init deferred: %s", exc)
    return app


if __name__ == "__main__":
    create_app().run(host="0.0.0.0", port=config.APP_PORT)
