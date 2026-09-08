#!/usr/bin/env python3
"""Push the operational scripts, compose file, nginx template and .env onto the
instance.

Run it after provisioning, and again whenever a script changes. It is separate
from deploy.py deliberately: shipping a new application version and changing
the machinery that ships it are different risks, and should be different
actions.

The .env is copied but never committed - it holds the MySQL passwords. It goes
to the box with 600 permissions and is the only place those secrets live.

Usage:
    python deploy/sync_scripts.py
    python deploy/sync_scripts.py --no-env     # scripts only, leave .env alone
"""
from __future__ import annotations

import argparse
import sys
import tarfile
import tempfile
from pathlib import Path

from common import REMOTE_ROOT, ROOT, fail, load_config, log, scp, ssh

PAYLOAD = [
    ("scripts", "scripts"),
    ("deploy/nginx", "deploy/nginx"),
    ("docker-compose.data.yml", "docker-compose.data.yml"),
]


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync scripts and config to the EC2 instance")
    parser.add_argument("--no-env", action="store_true", help="do not copy .env")
    args = parser.parse_args()

    cfg = load_config()
    log(f"target: {cfg['user']}@{cfg['host']}")

    # A tarball is one round trip instead of dozens, and - the part that
    # actually matters - it preserves the executable bit on the .sh files even
    # when the sync runs from Windows, where the filesystem has no such bit.
    with tempfile.TemporaryDirectory() as tmpdir:
        bundle = Path(tmpdir) / "payload.tar.gz"
        with tarfile.open(bundle, "w:gz") as tar:
            for local_rel, remote_rel in PAYLOAD:
                local = ROOT / local_rel
                if not local.exists():
                    fail(f"missing {local_rel} - run this from a full checkout")

                def make_executable(info: tarfile.TarInfo) -> tarfile.TarInfo:
                    if info.name.endswith(".sh"):
                        info.mode = 0o755
                    return info

                tar.add(local, arcname=remote_rel, filter=make_executable)

        ssh(cfg, f"mkdir -p {REMOTE_ROOT}/state {REMOTE_ROOT}/backups")
        scp(cfg, bundle, f"{REMOTE_ROOT}/payload.tar.gz")
        ssh(cfg, f"tar -xzf {REMOTE_ROOT}/payload.tar.gz -C {REMOTE_ROOT} "
                 f"&& rm -f {REMOTE_ROOT}/payload.tar.gz")

    if not args.no_env:
        env_file = ROOT / ".env"
        if not env_file.exists():
            fail(".env not found. Copy .env.example to .env and fill it in first.")
        scp(cfg, env_file, f"{REMOTE_ROOT}/.env")
        # Contains the MySQL root password - nobody else on the box needs it.
        ssh(cfg, f"chmod 600 {REMOTE_ROOT}/.env")

    log("verifying the sync")
    ssh(cfg, f"ls -la {REMOTE_ROOT} && ls -la {REMOTE_ROOT}/scripts && "
             f"bash -n {REMOTE_ROOT}/scripts/lib.sh && echo 'scripts parse cleanly'")

    log("sync complete")
    log(f"next: ssh in and run {REMOTE_ROOT}/scripts/db/bootstrap_db.sh (first time only)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
