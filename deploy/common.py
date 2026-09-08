#!/usr/bin/env python3
"""Shared plumbing for the deploy scripts: SSH, config and S3 deployment state.

Two ideas hold this file together.

SSH over subprocess rather than a library. Jenkins agents and dev laptops both
already have OpenSSH, so this adds no dependency, and every remote command is
printed as it runs - when a deploy fails you can copy the exact line out of the
build log and run it by hand.

S3 as the deployment ledger. `deployments/last-good.json` is what makes the
rollback trustworthy: the pipeline never has to guess which image was stable,
because a successful health check is the only thing that ever writes that file.
"""
from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "infra" / "outputs.json"
REMOTE_ROOT = "/opt/zerodown"

# The Name tag provision.py puts on the instance. Discovery keys off this.
INSTANCE_NAME = "zerodownpipeline-app"

HISTORY_KEY = "deployments/history.json"
LAST_GOOD_KEY = "deployments/last-good.json"
MAX_HISTORY_ENTRIES = 200


def log(msg: str) -> None:
    print(f"[deploy] {msg}", flush=True)


# Exit codes are part of this script's contract with Jenkins, because the
# difference between them is the difference between "your users are fine" and
# "your users are not":
#   0  deployed and recorded
#   1  health check failed - the new version was destroyed, traffic never moved
#   2  deployed and LIVE, but the S3 ledger write failed (bookkeeping is stale)
#   3  failed before any traffic change - bad config, unreachable host, etc.
#
# Only deploy.py's rollback path may exit 1. Everything else that goes wrong
# exits 3, so a build log can never claim a rollback that did not happen.
EXIT_ROLLED_BACK = 1
EXIT_LEDGER_STALE = 2
EXIT_PRECONDITION = 3


def fail(msg: str) -> None:
    print(f"[deploy] ERROR: {msg}", file=sys.stderr, flush=True)
    raise SystemExit(EXIT_PRECONDITION)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def load_env_file(path: Path | None = None) -> dict:
    path = path or (ROOT / ".env")
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip("\"'")
    return values


def discover_host(region: str) -> str:
    """Ask AWS for the public IP of the running instance, by tag.

    This is the last resort, used when nothing else supplied a host - which is
    the normal case in Jenkins, because infra/outputs.json is gitignored and a
    build works from a clean checkout.

    The alternative was a hand-copied EC2_HOST in the Jenkins settings, and a
    hand-copied address is a note that goes stale the moment the instance is
    replaced. The same reasoning already applies elsewhere here: active_color()
    reads the nginx config rather than a state file, and rollback.py asks the
    live service its version rather than believing the ledger. Ask reality.

    Returns "" rather than raising, so the caller can produce one good error
    message instead of a traceback. A missing permission, absent credentials or
    no running instance all mean the same thing to the caller: no host.
    """
    try:
        import boto3
        ec2 = boto3.client("ec2", region_name=region)
        reservations = ec2.describe_instances(Filters=[
            {"Name": "tag:Name", "Values": [INSTANCE_NAME]},
            {"Name": "instance-state-name", "Values": ["running"]},
        ])["Reservations"]
    except Exception as exc:  # noqa: BLE001 - any failure here means "no host"
        log(f"could not ask AWS which instance is running ({exc})")
        return ""

    found = [
        (i["InstanceId"], i["PublicIpAddress"])
        for res in reservations
        for i in res["Instances"]
        if i.get("PublicIpAddress")
    ]
    if not found:
        return ""
    if len(found) > 1:
        # Deploying to whichever one AWS happened to list first is how you
        # discover, later, that production was never updated.
        listed = ", ".join(f"{iid} ({ip})" for iid, ip in found)
        fail(f"found {len(found)} running instances tagged {INSTANCE_NAME}: "
             f"{listed}. Set EC2_HOST explicitly to name the one you mean.")

    instance_id, ip = found[0]
    log(f"discovered {ip} from AWS ({instance_id}, tag Name={INSTANCE_NAME})")
    return ip


def load_config() -> dict:
    """Merge infra/outputs.json, .env and the real environment.

    Precedence is environment > .env > outputs.json, so Jenkins credentials and
    build parameters override whatever is committed or generated locally.
    """
    outputs = {}
    if OUTPUTS.exists():
        outputs = json.loads(OUTPUTS.read_text(encoding="utf-8"))
    env_file = load_env_file()

    def pick(*names, default=""):
        for name in names:
            if os.environ.get(name):
                return os.environ[name]
        for name in names:
            if env_file.get(name):
                return env_file[name]
        for name in names:
            if outputs.get(name):
                return outputs[name]
        return default

    key_path = pick("EC2_KEY_PATH", "key_path", default="")
    if key_path and not Path(key_path).is_absolute():
        key_path = str(ROOT / key_path)

    region = pick("AWS_REGION", "region", default="ap-south-1")

    cfg = {
        "host": pick("EC2_HOST", "public_ip"),
        "user": pick("EC2_USER", default="ec2-user"),
        "key_path": key_path,
        "region": region,
        "bucket": pick("S3_BUCKET", "s3_bucket"),
        "dockerhub_user": pick("DOCKERHUB_USER"),
        "image_name": pick("DOCKER_IMAGE", default="zerodownpipeline-api"),
        "blue_port": int(pick("APP_PORT_BLUE", default="8000")),
        "green_port": int(pick("APP_PORT_GREEN", default="8001")),
    }

    # Nothing named a host, so ask AWS. EC2_HOST stays ahead of this on purpose:
    # discovery is the convenience, an explicit setting is still the override.
    if not cfg["host"]:
        cfg["host"] = discover_host(region)

    if not cfg["host"]:
        fail(f"no EC2 host, and no running instance tagged Name={INSTANCE_NAME} "
             f"in {region}. Run infra/provision.py first, or set EC2_HOST.")
    return cfg


def image_ref(cfg: dict, tag: str) -> str:
    return f"{cfg['dockerhub_user']}/{cfg['image_name']}:{tag}"


# --------------------------------------------------------------------------- #
# ssh
# --------------------------------------------------------------------------- #
def _ssh_base(cfg: dict) -> list[str]:
    cmd = [
        "ssh",
        # The host key is unknown on a freshly provisioned box and Jenkins has
        # no human to answer the prompt. Acceptable here because the host is
        # one we just created ourselves and addressed by IP from outputs.json.
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=15",
        "-o", "BatchMode=yes",
    ]
    if cfg.get("key_path"):
        cmd += ["-i", cfg["key_path"]]
    return cmd


def ssh(cfg: dict, command: str, check: bool = True, capture: bool = False,
        quiet: bool = False) -> subprocess.CompletedProcess:
    """Run one command on the instance. Remote output streams into the build log."""
    remote = f"cd {REMOTE_ROOT} && {command}"
    argv = _ssh_base(cfg) + [f"{cfg['user']}@{cfg['host']}", remote]
    if not quiet:
        log(f"ssh: {command}")
    result = subprocess.run(
        argv,
        text=True,
        capture_output=capture,
        check=False,
    )
    if check and result.returncode != 0:
        if capture and result.stderr:
            print(result.stderr, file=sys.stderr)
        fail(f"remote command failed (exit {result.returncode}): {command}")
    return result


def ssh_output(cfg: dict, command: str) -> str:
    result = ssh(cfg, command, check=True, capture=True, quiet=True)
    return result.stdout.strip()


def scp(cfg: dict, local: Path, remote_path: str, recursive: bool = False) -> None:
    argv = [
        "scp",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=15",
        "-o", "BatchMode=yes",
    ]
    if recursive:
        argv.append("-r")
    if cfg.get("key_path"):
        argv += ["-i", cfg["key_path"]]
    argv += [str(local), f"{cfg['user']}@{cfg['host']}:{remote_path}"]
    log(f"scp: {local.name} -> {remote_path}")
    result = subprocess.run(argv, text=True, check=False)
    if result.returncode != 0:
        fail(f"scp failed for {local}")


def remote_script(cfg: dict, script: str, *args: str) -> subprocess.CompletedProcess:
    """Invoke one of the bash scripts on the instance, with arguments quoted."""
    quoted = " ".join(shlex.quote(str(a)) for a in args)
    return ssh(cfg, f"{REMOTE_ROOT}/{script} {quoted}".strip())


# --------------------------------------------------------------------------- #
# blue-green state
# --------------------------------------------------------------------------- #
def active_color(cfg: dict) -> str:
    return ssh_output(cfg, f". {REMOTE_ROOT}/scripts/lib.sh && active_color")


def inactive_color(cfg: dict) -> str:
    return ssh_output(cfg, f". {REMOTE_ROOT}/scripts/lib.sh && inactive_color")


def port_for(cfg: dict, color: str) -> int:
    return cfg["blue_port"] if color == "blue" else cfg["green_port"]


# --------------------------------------------------------------------------- #
# S3 deployment ledger
# --------------------------------------------------------------------------- #
def s3_client(cfg: dict):
    import boto3  # imported lazily so `--help` works without AWS deps
    return boto3.client("s3", region_name=cfg["region"])


def read_json_key(cfg: dict, key: str, default):
    from botocore.exceptions import ClientError
    try:
        obj = s3_client(cfg).get_object(Bucket=cfg["bucket"], Key=key)
        return json.loads(obj["Body"].read())
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NoSuchKey", "404"):
            return default
        raise


def write_json_key(cfg: dict, key: str, data) -> None:
    s3_client(cfg).put_object(
        Bucket=cfg["bucket"],
        Key=key,
        Body=json.dumps(data, indent=2).encode(),
        ContentType="application/json",
    )


def append_history(cfg: dict, entry: dict) -> None:
    """Append one deploy attempt to the ledger - successes and failures alike.

    Failed deploys are recorded on purpose. A history that only contains
    successes cannot answer 'how often does this break, and how fast do we
    recover', which is the number this project exists to produce.
    """
    history = read_json_key(cfg, HISTORY_KEY, default=[])
    if not isinstance(history, list):
        history = []
    history.append(entry)
    write_json_key(cfg, HISTORY_KEY, history[-MAX_HISTORY_ENTRIES:])
    log(f"recorded '{entry.get('status')}' in s3://{cfg['bucket']}/{HISTORY_KEY}")


def read_last_good(cfg: dict):
    return read_json_key(cfg, LAST_GOOD_KEY, default=None)


def write_last_good(cfg: dict, image_tag: str, color: str, port: int) -> None:
    """Only ever called after a health check passed AND traffic was switched."""
    write_json_key(cfg, LAST_GOOD_KEY, {
        "image_tag": image_tag,
        "color": color,
        "port": port,
        "promoted_at": now_iso(),
    })
    log(f"last-good is now {image_tag} ({color}:{port})")
