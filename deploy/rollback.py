#!/usr/bin/env python3
"""Roll back to a previously known-good image.

deploy.py already handles the common failure: a build that cannot pass its
health check never gets traffic, so there is nothing to undo. This script
covers the other case - a version that started cleanly, was promoted, and only
then turned out to be wrong.

It does not take a shortcut just because the target is "known good". The old
image goes through exactly the same path a new one does: start in the inactive
slot, health-check it, and only switch traffic if it passes. A rollback that
skips verification is just another unverified deploy.

Which version to go back to comes from the S3 ledger, never from memory:
  --tag X       roll back to X
  (no args)     roll back to the most recent successful deploy that is not
                the one currently live

Usage:
    python deploy/rollback.py                 # previous known-good version
    python deploy/rollback.py --tag a1b2c3d   # a specific version
    python deploy/rollback.py --list          # show recent deploy history
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from common import (
    HISTORY_KEY,
    REMOTE_ROOT,
    active_color,
    append_history,
    fail,
    image_ref,
    inactive_color,
    load_config,
    log,
    now_iso,
    port_for,
    read_json_key,
    read_last_good,
    remote_script,
    ssh,
    write_last_good,
)


def live_version_of(cfg: dict) -> str:
    """What the public endpoint actually reports right now.

    Deliberately best-effort: if the service is unreachable this returns "" and
    the stale-ledger check is skipped, because an unreachable service is exactly
    when a rollback is most needed.
    """
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{cfg['host']}/health", timeout=8) as resp:
            body = json.loads(resp.read())
        return body.get("version", "")
    except Exception as exc:  # noqa: BLE001
        log(f"could not read the live version ({exc}) - skipping the staleness check")
        return ""


def show_history(cfg: dict, limit: int = 15) -> None:
    history = read_json_key(cfg, HISTORY_KEY, default=[])
    if not history:
        log("no deployment history in S3 yet")
        return
    last_good = read_last_good(cfg) or {}
    live_tag = last_good.get("image_tag", "")

    print(f"\n  {'when':<22} {'tag':<12} {'status':<9} {'colour':<7} secs")
    print("  " + "-" * 62)
    for entry in history[-limit:]:
        when = (entry.get("deployed_at") or entry.get("attempted_at") or "")[:19]
        tag = entry.get("image_tag", "?")
        marker = " <- live" if tag == live_tag else ""
        print(f"  {when:<22} {tag:<12} {entry.get('status', '?'):<9} "
              f"{entry.get('live_color', '?'):<7} {entry.get('total_seconds', '?')}{marker}")
    print()


def previous_good_tag(cfg: dict) -> str:
    history = read_json_key(cfg, HISTORY_KEY, default=[])
    last_good = read_last_good(cfg) or {}
    live_tag = last_good.get("image_tag", "")

    successes = [e for e in history if e.get("status") == "success"]
    for entry in reversed(successes):
        if entry.get("image_tag") and entry["image_tag"] != live_tag:
            return entry["image_tag"]
    fail("no earlier successful deploy found in the S3 history - "
         "there is nothing to roll back to. Pass --tag explicitly if you know one.")
    return ""  # unreachable; keeps type checkers happy


def main() -> int:
    parser = argparse.ArgumentParser(description="Roll back to a known-good image version")
    parser.add_argument("--tag", help="specific image tag to roll back to")
    parser.add_argument("--list", action="store_true", help="print recent deploy history and exit")
    parser.add_argument("--retries", type=int, default=10)
    parser.add_argument("--interval", type=int, default=3)
    parser.add_argument("--grace", type=int, default=15)
    args = parser.parse_args()

    cfg = load_config()

    if args.list:
        show_history(cfg)
        return 0

    # Before trusting the ledger, check it against reality. deploy.py can leave
    # last-good.json stale if the traffic switch succeeded but the S3 write did
    # not (exit code 2). Rolling back on a stale ledger would "restore" a
    # version OLDER than the one currently serving - the safety mechanism
    # causing the outage. So compare what is live with what the ledger claims.
    live_version = live_version_of(cfg)
    last_good = read_last_good(cfg) or {}
    recorded = last_good.get("image_tag", "")
    if live_version and recorded and live_version != recorded:
        err_msg = "\n".join([
            f"LEDGER IS STALE: the live service reports '{live_version}' but "
            f"last-good.json says '{recorded}'.",
            "Rolling back now could replace the running version with an older one.",
            f"Reconcile first:  python deploy/deploy.py --tag {live_version}",
            "Or, if you are certain, name the target explicitly with --tag.",
        ])
        if not args.tag:
            fail(err_msg)
        log("WARNING: ledger is stale, but --tag was given explicitly - proceeding")
        log(f"  live={live_version}  ledger={recorded}  rolling back to={args.tag}")

    tag = args.tag or previous_good_tag(cfg)
    image = image_ref(cfg, tag)

    started = time.time()
    current = active_color(cfg)
    target = inactive_color(cfg)
    target_port = port_for(cfg, target)

    log("=" * 68)
    log(f"ROLLING BACK to {image}")
    log(f"live colour: {current} -> restoring into: {target} (port {target_port})")
    log("=" * 68)

    log(f"[1/3] starting {tag} in the {target} slot")
    remote_script(cfg, "scripts/start_container.sh", target, image, tag, "0")

    log(f"[2/3] verifying {tag} before giving it traffic")
    result = ssh(
        cfg,
        f"{REMOTE_ROOT}/scripts/healthcheck.sh {target_port} {tag} {args.retries} {args.interval}",
        check=False,
    )
    if result.returncode != 0:
        remote_script(cfg, "scripts/stop_container.sh", target, "1")
        append_history(cfg, {
            "image_tag": tag,
            "image": image,
            "attempted_at": now_iso(),
            "status": "failed",
            "health_check": "failed",
            "operation": "rollback",
            "live_color": active_color(cfg),
            "total_seconds": round(time.time() - started, 1),
        })
        fail(f"the rollback target {tag} did not pass its own health check. "
             f"Traffic is unchanged (still {current}). "
             f"Check the database and Redis before trying another version.")

    log(f"[3/3] switching traffic back to {tag}")
    remote_script(cfg, "scripts/switch_traffic.sh", target)
    if current not in ("none", target):
        remote_script(cfg, "scripts/stop_container.sh", current, str(args.grace))

    write_last_good(cfg, tag, target, target_port)

    total = round(time.time() - started, 1)
    append_history(cfg, {
        "image_tag": tag,
        "image": image,
        "deployed_at": now_iso(),
        "status": "success",
        "health_check": "passed",
        "operation": "rollback",
        "previous_color": current,
        "live_color": target,
        "live_port": target_port,
        "total_seconds": total,
    })

    log("=" * 68)
    log(f"ROLLBACK COMPLETE - {tag} is live on {target}:{target_port} in {total}s")
    log("=" * 68)
    return 0


if __name__ == "__main__":
    sys.exit(main())
