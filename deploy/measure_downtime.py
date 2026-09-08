#!/usr/bin/env python3
"""Prove the "zero downtime" claim instead of asserting it.

Run this in one terminal, run a deploy in another. It sends continuous requests
to the live endpoint and reports how many failed and what the longest gap in
service was. If the blue-green switch works, the answer is zero failures - and
you will see the X-Served-By header flip mid-run, which is the moment traffic
moved from one container to the other.

This is where the resume number comes from. "Zero downtime" with a request log
behind it is a fact; without one it is a hope.

Usage:
    python deploy/measure_downtime.py --duration 120
    python deploy/measure_downtime.py --duration 120 --rps 10 --out docs/downtime-run.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from common import load_config, log


def probe(url: str, timeout: float = 5.0):
    """One request. Returns (ok, status_or_error, latency_ms, served_by)."""
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            latency = (time.perf_counter() - start) * 1000
            resp.read()
            return True, resp.status, latency, resp.headers.get("X-Served-By", "?")
    except urllib.error.HTTPError as exc:
        return False, exc.code, (time.perf_counter() - start) * 1000, "?"
    except Exception as exc:  # noqa: BLE001 - a connection refused IS the outage
        return False, type(exc).__name__, (time.perf_counter() - start) * 1000, "?"


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure downtime across a deploy")
    parser.add_argument("--host", help="override the host from infra/outputs.json")
    parser.add_argument("--path", default="/health", help="path to poll (default /health)")
    parser.add_argument("--duration", type=int, default=120, help="seconds to run")
    parser.add_argument("--rps", type=float, default=5.0, help="requests per second")
    parser.add_argument("--out", help="write the full result as JSON to this path")
    args = parser.parse_args()

    cfg = load_config()
    host = args.host or cfg["host"]
    url = f"http://{host}{args.path}"
    interval = 1.0 / args.rps

    log(f"probing {url} at ~{args.rps} req/s for {args.duration}s")
    log("start your deploy now - watch the served_by column flip")
    print(f"\n  {'elapsed':>8}  {'result':<14} {'ms':>7}  served_by")
    print("  " + "-" * 46)

    started = time.time()
    deadline = started + args.duration
    total = 0
    failures = []
    latencies = []
    colors = Counter()
    last_color = None
    switch_at = None
    longest_gap = 0.0
    gap_start = None

    while time.time() < deadline:
        loop_start = time.time()
        ok, status, latency, served_by = probe(url)
        total += 1
        elapsed = round(loop_start - started, 1)

        if ok:
            latencies.append(latency)
            colors[served_by] += 1
            if gap_start is not None:
                longest_gap = max(longest_gap, loop_start - gap_start)
                gap_start = None
            if last_color is not None and served_by != last_color and served_by != "?":
                switch_at = elapsed
                print(f"  {elapsed:>8}  {'SWITCHED':<14} {latency:>7.0f}  "
                      f"{last_color} -> {served_by}")
            last_color = served_by
        else:
            if gap_start is None:
                gap_start = loop_start
            failures.append({"at_s": elapsed, "error": str(status)})
            print(f"  {elapsed:>8}  {'FAIL ' + str(status):<14} {latency:>7.0f}  -")

        # Only print successes occasionally - failures and switches are the signal.
        if ok and total % max(1, int(args.rps * 10)) == 0:
            print(f"  {elapsed:>8}  {'ok ' + str(status):<14} {latency:>7.0f}  {served_by}")

        sleep_for = interval - (time.time() - loop_start)
        if sleep_for > 0:
            time.sleep(sleep_for)

    if gap_start is not None:
        longest_gap = max(longest_gap, time.time() - gap_start)

    latencies.sort()

    def pct(p: float) -> float:
        if not latencies:
            return 0.0
        return round(latencies[min(len(latencies) - 1, int(len(latencies) * p))], 1)

    result = {
        "url": url,
        "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "duration_seconds": args.duration,
        "target_rps": args.rps,
        "requests_total": total,
        "requests_failed": len(failures),
        "availability_percent": round(100 * (total - len(failures)) / total, 4) if total else 0,
        "longest_outage_seconds": round(longest_gap, 2),
        "latency_ms": {"p50": pct(0.50), "p95": pct(0.95), "p99": pct(0.99),
                       "max": round(latencies[-1], 1) if latencies else 0},
        "served_by_counts": dict(colors),
        "traffic_switch_observed_at_s": switch_at,
        "failures": failures[:50],
    }

    print("\n" + "=" * 60)
    print(f"  requests            : {result['requests_total']}")
    print(f"  failed              : {result['requests_failed']}")
    print(f"  availability        : {result['availability_percent']}%")
    print(f"  longest outage      : {result['longest_outage_seconds']}s")
    print(f"  latency p50/p95/max : {result['latency_ms']['p50']} / "
          f"{result['latency_ms']['p95']} / {result['latency_ms']['max']} ms")
    print(f"  served by           : {result['served_by_counts']}")
    if switch_at is not None:
        print(f"  traffic switched at : {switch_at}s into the run")
    else:
        print("  traffic switched at : not observed (no deploy during this window?)")
    print("=" * 60)

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        log(f"wrote {out}")

    if result["requests_failed"] == 0 and switch_at is not None:
        log("ZERO failed requests across a live traffic switch. That is the claim, measured.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
