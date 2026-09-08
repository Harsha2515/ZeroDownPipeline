#!/usr/bin/env python3
"""Post-deploy smoke test against the PUBLIC endpoint.

scripts/healthcheck.sh runs on the instance and checks a single container
directly on its loopback port - that is the gate that decides promote or
rollback. This script is the outside-in counterpart: it goes through nginx from
wherever Jenkins is running, so it also proves the proxy, the security group
and the traffic switch are all correct.

It checks behaviour, not just liveness: creating a short link and following the
redirect exercises MySQL, Redis and the whole request path. A /health endpoint
returning 200 while the app cannot actually serve a request is exactly the
failure this catches.

Usage:
    python deploy/healthcheck.py                    # smoke test the live host
    python deploy/healthcheck.py --expect-version a1b2c3d
    python deploy/healthcheck.py --host 1.2.3.4
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from common import fail, load_config, log


def request(url: str, method: str = "GET", payload: dict | None = None,
            allow_redirects: bool = True, timeout: int = 10):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener() if allow_redirects else \
        urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read().decode(errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test the deployed service")
    parser.add_argument("--host", help="override the host from infra/outputs.json")
    parser.add_argument("--expect-version", help="fail unless /health reports this version")
    args = parser.parse_args()

    cfg = load_config()
    host = args.host or cfg["host"]
    base = f"http://{host}"
    failures = []

    # 1. health
    log(f"GET {base}/health")
    status, headers, body = request(f"{base}/health")
    if status != 200:
        failures.append(f"/health returned {status}: {body[:200]}")
    else:
        health = json.loads(body)
        served_by = headers.get("X-Served-By", "?")
        log(f"  status={health.get('status')} version={health.get('version')} "
            f"served_by={served_by} checks={health.get('checks')}")
        if args.expect_version and health.get("version") != args.expect_version:
            failures.append(
                f"live version is {health.get('version')}, expected {args.expect_version} - "
                f"nginx may still be pointing at the old container"
            )
        if health.get("status") == "degraded":
            log("  WARNING: app is degraded - a non-critical dependency is down")

    # 2. create a link (MySQL write + Redis write + S3 write)
    log(f"POST {base}/shorten")
    status, _, body = request(f"{base}/shorten", "POST", {"url": "https://example.com/smoke-test"})
    code = None
    if status != 201:
        failures.append(f"/shorten returned {status}: {body[:200]}")
    else:
        code = json.loads(body)["short_code"]
        log(f"  created {code}")

    # 3. follow it (Redis read, then MySQL write for the click counter)
    if code:
        log(f"GET {base}/{code}")
        status, headers, _ = request(f"{base}/{code}", allow_redirects=False)
        if status != 302:
            failures.append(f"redirect returned {status}, expected 302")
        elif headers.get("Location") != "https://example.com/smoke-test":
            failures.append(f"redirect went to {headers.get('Location')}")
        else:
            log("  302 to the right place")

        # 4. stats (replica read - proves replication is live, not just configured)
        log(f"GET {base}/stats/{code}")
        status, _, body = request(f"{base}/stats/{code}")
        if status != 200:
            failures.append(f"/stats returned {status}: {body[:200]}")
        else:
            stats = json.loads(body)
            if stats.get("clicks", 0) < 1:
                failures.append("click count did not increment - replica may be lagging or stale")
            else:
                log(f"  clicks={stats['clicks']} (read from the replica)")

    if failures:
        for f in failures:
            print(f"[deploy] FAILED: {f}", file=sys.stderr)
        fail(f"smoke test failed with {len(failures)} problem(s)")

    log("smoke test passed - the deployed service works end to end")
    return 0


if __name__ == "__main__":
    sys.exit(main())
