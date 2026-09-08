#!/usr/bin/env python3
"""Destroy everything provision.py created.

Being able to delete the whole environment on demand is what makes a free-tier
project safe to leave alone: nothing keeps running because you forgot about it.

Deletion order is forced by AWS dependencies - the instance holds the security
group and the instance profile, so it has to go first, and the script waits for
termination rather than firing all the deletes and hoping.

The S3 bucket is treated separately and more carefully than the compute,
because the compute is disposable and the data is not.

Usage:
    python infra/teardown.py                    # terminate EC2, keep S3 + IAM
    python infra/teardown.py --all              # also delete IAM role, SG, key
    python infra/teardown.py --all --delete-bucket --confirm   # everything, data included
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUTPUTS = HERE / "outputs.json"

SG_NAME = "zerodownpipeline-sg"
ROLE_NAME = "zerodownpipeline-ec2-role"
PROFILE_NAME = "zerodownpipeline-ec2-profile"
INSTANCE_NAME = "zerodownpipeline-app"


def log(msg: str) -> None:
    print(f"[teardown] {msg}", flush=True)


def load_outputs() -> dict:
    if not OUTPUTS.exists():
        raise SystemExit(
            "infra/outputs.json not found - nothing recorded to tear down.\n"
            "If resources exist but the file is gone, delete them from the console."
        )
    return json.loads(OUTPUTS.read_text(encoding="utf-8"))


def terminate_instances(ec2) -> None:
    reservations = ec2.describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [INSTANCE_NAME]},
        {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]},
    ])["Reservations"]
    ids = [i["InstanceId"] for r in reservations for i in r["Instances"]]

    if not ids:
        log("no instances to terminate")
        return

    log(f"terminating {', '.join(ids)}")
    ec2.terminate_instances(InstanceIds=ids)
    log("waiting for termination (the security group cannot be deleted until this finishes)")
    ec2.get_waiter("instance_terminated").wait(InstanceIds=ids)
    log("instances terminated")


def delete_security_group(ec2, sg_id: str) -> None:
    if not sg_id:
        return
    # The ENI can linger for a few seconds after termination, so retry rather
    # than reporting a failure the user would only have to re-run.
    for attempt in range(1, 7):
        try:
            ec2.delete_security_group(GroupId=sg_id)
            log(f"deleted security group {sg_id}")
            return
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code in ("InvalidGroup.NotFound",):
                log(f"security group {sg_id} already gone")
                return
            if code == "DependencyViolation" and attempt < 6:
                log(f"security group still in use, retrying ({attempt}/6)")
                time.sleep(10)
                continue
            raise


def delete_key_pair(ec2, key_name: str) -> None:
    if not key_name:
        return
    ec2.delete_key_pair(KeyName=key_name)
    log(f"deleted key pair {key_name}")
    local = ROOT / f"{key_name}.pem"
    if local.exists():
        local.unlink()
        log(f"removed local {local.name}")


def delete_iam(iam) -> None:
    try:
        iam.remove_role_from_instance_profile(InstanceProfileName=PROFILE_NAME, RoleName=ROLE_NAME)
        log("detached role from instance profile")
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "NoSuchEntity":
            raise
    for fn, kwargs, label in (
        (iam.delete_instance_profile, {"InstanceProfileName": PROFILE_NAME}, "instance profile"),
        (iam.delete_role_policy,
         {"RoleName": ROLE_NAME, "PolicyName": "zerodownpipeline-s3-access"}, "inline policy"),
        (iam.delete_role, {"RoleName": ROLE_NAME}, "role"),
    ):
        try:
            fn(**kwargs)
            log(f"deleted {label}")
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "NoSuchEntity":
                log(f"{label} already gone")
            else:
                raise


def empty_and_delete_bucket(s3, bucket: str) -> None:
    """A versioned bucket is not empty until every version AND every delete
    marker is gone - deleting the visible objects is not enough."""
    log(f"emptying s3://{bucket} (all versions and delete markers)")
    paginator = s3.get_paginator("list_object_versions")
    deleted = 0
    for page in paginator.paginate(Bucket=bucket):
        objects = [
            {"Key": item["Key"], "VersionId": item["VersionId"]}
            for group in ("Versions", "DeleteMarkers")
            for item in page.get(group, [])
        ]
        for i in range(0, len(objects), 1000):
            batch = objects[i:i + 1000]
            s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
            deleted += len(batch)
    log(f"deleted {deleted} object versions")
    s3.delete_bucket(Bucket=bucket)
    log(f"deleted bucket {bucket}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Destroy ZeroDownPipeline AWS infrastructure")
    parser.add_argument("--all", action="store_true",
                        help="also delete the security group, key pair and IAM role")
    parser.add_argument("--delete-bucket", action="store_true",
                        help="also delete the S3 bucket AND EVERYTHING IN IT")
    parser.add_argument("--confirm", action="store_true",
                        help="required alongside --delete-bucket")
    args = parser.parse_args()

    outputs = load_outputs()
    region = outputs.get("region", "ap-south-1")
    bucket = outputs.get("s3_bucket", "")

    session = boto3.Session(region_name=region)
    ec2 = session.client("ec2")
    iam = session.client("iam")
    s3 = session.client("s3")

    terminate_instances(ec2)

    if args.all:
        delete_security_group(ec2, outputs.get("security_group_id", ""))
        delete_key_pair(ec2, outputs.get("key_name", ""))
        delete_iam(iam)
    else:
        log("keeping security group, key pair and IAM role (pass --all to remove them)")

    if args.delete_bucket:
        if not args.confirm:
            log(f"REFUSING to delete s3://{bucket}: --delete-bucket also needs --confirm.")
            log("That bucket holds your records and your database backups.")
            return 1
        empty_and_delete_bucket(s3, bucket)
    elif bucket:
        log(f"keeping s3://{bucket} - it costs approximately nothing and holds your backups")

    if args.all and args.delete_bucket:
        OUTPUTS.unlink(missing_ok=True)
        log("removed infra/outputs.json")

    log("teardown complete")
    return 0


if __name__ == "__main__":
    sys.exit(main())
