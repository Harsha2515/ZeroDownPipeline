#!/usr/bin/env python3
"""Deploy one image version with a blue-green switch and automatic rollback.

The sequence, and why each step is where it is:

  1. Work out which colour is INACTIVE. Everything destructive in this script
     happens only to that slot, so a failure at any point before step 4 cannot
     touch the container currently serving users.
  2. Start the new version there. Live traffic still goes to the old one.
  3. Health-check the new container directly on its port, bypassing nginx.
     The check requires the reported version to match what we just deployed,
     so a container that failed to start and left the old one on that port
     cannot be mistaken for a success.
  4. PASS -> switch nginx (graceful reload, no dropped connections), stop the
     old container after a grace period, and only now update last-good in S3.
     FAIL -> tear down the new container and stop. The old one was never
     touched, so there is nothing to restore and nothing to wait for. That is
     the rollback: it is fast because it is a no-op on the live path.

Every attempt, successful or not, is appended to the S3 deployment history with
its measured durations. Those are the numbers that go on the resume.

Usage:
    python deploy/deploy.py --tag a1b2c3d
    python deploy/deploy.py --tag a1b2c3d --break-health   # rollback drill
"""
from __future__ import annotations

import argparse
import sys
import time

from common import (
    EXIT_LEDGER_STALE,
    EXIT_ROLLED_BACK,
    REMOTE_ROOT,
    active_color,
    append_history,
    image_ref,
    inactive_color,
    load_config,
    log,
    now_iso,
    port_for,
    remote_script,
    ssh,
    write_last_good,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Blue-green deploy with automatic rollback")
    parser.add_argument("--tag", required=True, help="image tag to deploy (git short sha)")
    parser.add_argument("--retries", type=int, default=10, help="health check attempts")
    parser.add_argument("--interval", type=int, default=3, help="seconds between attempts")
    parser.add_argument("--grace", type=int, default=15,
                        help="seconds to let the old container drain after the switch")
    parser.add_argument("--break-health", action="store_true",
                        help="deploy with BREAK_HEALTHCHECK=1 to prove the rollback works")
    args = parser.parse_args()

    cfg = load_config()
    image = image_ref(cfg, args.tag)

    started = time.time()
    marks: dict[str, float] = {}

    def mark(name: str) -> None:
        marks[name] = round(time.time() - started, 1)

    log("=" * 68)
    log(f"deploying {image}")
    log(f"target    {cfg['user']}@{cfg['host']}")
    if args.break_health:
        log("MODE: rollback drill - this build is expected to FAIL its health check")
    log("=" * 68)

    # --- 1. pick the slot ---------------------------------------------------
    current = active_color(cfg)
    target = inactive_color(cfg)
    target_port = port_for(cfg, target)
    log(f"live colour: {current} -> deploying into: {target} (port {target_port})")

    # --- 2. start the new container ----------------------------------------
    log(f"[1/4] starting {target} container")
    remote_script(cfg, "scripts/start_container.sh", target, image, args.tag,
                  "1" if args.break_health else "0")
    mark("container_started_s")

    # --- 3. health check ----------------------------------------------------
    log(f"[2/4] health-checking {target} on port {target_port}")
    health_start = time.time()
    result = ssh(
        cfg,
        f"{REMOTE_ROOT}/scripts/healthcheck.sh {target_port} {args.tag} "
        f"{args.retries} {args.interval}",
        check=False,
    )
    healthy = result.returncode == 0
    health_seconds = round(time.time() - health_start, 1)
    mark("health_checked_s")

    # --- 4a. rollback -------------------------------------------------------
    if not healthy:
        log("=" * 68)
        log("HEALTH CHECK FAILED - rolling back automatically")
        log("=" * 68)
        log(f"[3/4] tearing down the failed {target} container")
        # No grace period: this container never received a user request.
        remote_script(cfg, "scripts/stop_container.sh", target, "1")
        mark("rolled_back_s")

        still_live = active_color(cfg)
        log(f"[4/4] live colour is still '{still_live}' - users were never affected")

        append_history(cfg, {
            "image_tag": args.tag,
            "image": image,
            "attempted_at": now_iso(),
            "status": "failed",
            "health_check": "failed",
            "target_color": target,
            "live_color": still_live,
            "rollback": "automatic",
            "health_check_seconds": health_seconds,
            "time_to_rollback_seconds": marks["rolled_back_s"],
            "total_seconds": round(time.time() - started, 1),
            "deliberate_drill": args.break_health,
        })

        log(f"time from deploy start to completed rollback: {marks['rolled_back_s']}s")
        if args.break_health:
            log("drill succeeded: the pipeline caught a bad build and recovered on its own.")
        # The ONLY place that returns this code. It means, specifically: a new
        # version was started, failed its health check, and was destroyed
        # without ever receiving traffic.
        return EXIT_ROLLED_BACK

    # --- 4b. promote --------------------------------------------------------
    log(f"[3/4] health check passed in {health_seconds}s - switching traffic to {target}")
    remote_script(cfg, "scripts/switch_traffic.sh", target)
    mark("traffic_switched_s")

    if current not in ("none", target):
        log(f"[4/4] draining and removing the old {current} container")
        remote_script(cfg, "scripts/stop_container.sh", current, str(args.grace))
    else:
        log("[4/4] no previous container to retire (first deploy)")
    mark("old_container_removed_s")

    # Everything below this line is bookkeeping. The deploy is already LIVE:
    # nginx has switched and the old container is gone. So a failure here must
    # never be reported as a rollback - it is a stale-ledger problem, and the
    # difference matters enormously, because rollback.py trusts last-good.json.
    # Exit code 2 says "deployed, but the record of it is wrong".
    ledger_error = None
    for attempt in range(1, 4):
        try:
            write_last_good(cfg, args.tag, target, target_port)
            ledger_error = None
            break
        except Exception as exc:  # noqa: BLE001
            ledger_error = exc
            log(f"ledger write failed (attempt {attempt}/3): {exc}")
            if attempt < 3:
                time.sleep(3)

    total = round(time.time() - started, 1)
    if ledger_error is not None:
        log("=" * 68)
        log(f"DEPLOY SUCCEEDED - {args.tag} IS LIVE on {target}:{target_port}")
        log("BUT the S3 deployment ledger could not be updated:")
        log(f"  {ledger_error}")
        log("")
        log("Nothing was rolled back. The new version is serving traffic.")
        log("last-good.json is now STALE - do NOT run rollback.py until it is")
        log("fixed, or it will 'restore' a version older than the live one:")
        log(f"  python deploy/deploy.py --tag {args.tag}    # re-run to reconcile")
        log("=" * 68)
        return EXIT_LEDGER_STALE

    append_history(cfg, {
        "image_tag": args.tag,
        "image": image,
        "deployed_at": now_iso(),
        "status": "success",
        "health_check": "passed",
        "previous_color": current,
        "live_color": target,
        "live_port": target_port,
        "health_check_seconds": health_seconds,
        "time_to_traffic_switch_seconds": marks["traffic_switched_s"],
        "total_seconds": total,
    })

    log("=" * 68)
    log(f"DEPLOY SUCCEEDED - {args.tag} is live on {target}:{target_port} in {total}s")
    log(f"  http://{cfg['host']}/health")
    log("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
