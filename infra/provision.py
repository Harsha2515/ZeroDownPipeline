#!/usr/bin/env python3
"""Create every AWS resource this project needs, from nothing, with boto3.

Run it as many times as you like: every step checks for what it would create
first, so a second run reports what already exists instead of failing or making
duplicates. That property is the whole point - infrastructure you can only
create once is infrastructure you are afraid of.

What it creates:
  * an S3 bucket (versioned, public access blocked, lifecycle-pruned)
  * an EC2 key pair, with the private key written next to this file
  * a security group (22 from your IP only, 80 from anywhere)
  * an IAM role + instance profile so the box can reach S3 without any
    long-lived access keys living on it
  * one t3.micro EC2 instance running infra/user_data.sh on first boot

Everything it learns is written to infra/outputs.json, which the deploy scripts
and Jenkins read. Nothing in this project asks you to copy an IP by hand.

Usage:
    python infra/provision.py                # create/verify everything
    python infra/provision.py --skip-instance  # AWS plumbing only, no EC2
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
import urllib.request
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
OUTPUTS = HERE / "outputs.json"
USER_DATA = HERE / "user_data.sh"

PROJECT_TAG = "ZeroDownPipeline"
SG_NAME = "zerodownpipeline-sg"
ROLE_NAME = "zerodownpipeline-ec2-role"
PROFILE_NAME = "zerodownpipeline-ec2-profile"
INSTANCE_NAME = "zerodownpipeline-app"
# Amazon publishes the current AL2023 AMI id per region as an SSM parameter,
# so this never needs a hardcoded, region-specific, quietly-stale AMI id.
AL2023_SSM_PARAM = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"

TAGS = [
    {"Key": "Project", "Value": PROJECT_TAG},
    {"Key": "ManagedBy", "Value": "provision.py"},
]


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(f"[provision] {msg}", flush=True)


def load_env() -> dict:
    """Read .env if present; real environment variables win over the file."""
    values = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("\"'")
    prefixes = ("AWS_", "S3_", "EC2_")
    values.update({k: v for k, v in os.environ.items()
                   if k in values or k.startswith(prefixes)})
    return values


def my_public_ip() -> str:
    """Used to scope SSH to this machine only. Falls back to open SSH with a
    loud warning rather than silently locking you out of your own box."""
    try:
        with urllib.request.urlopen("https://checkip.amazonaws.com", timeout=5) as resp:
            return resp.read().decode().strip()
    except Exception as exc:  # noqa: BLE001
        print(f"[provision] WARNING: could not detect your public IP ({exc}); "
              f"SSH will be open to 0.0.0.0/0. Tighten it in the console.", file=sys.stderr)
        return "0.0.0.0"


def read_outputs() -> dict:
    if OUTPUTS.exists():
        try:
            return json.loads(OUTPUTS.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def write_outputs(data: dict) -> None:
    merged = read_outputs()
    merged.update(data)
    OUTPUTS.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
    log(f"wrote {OUTPUTS.relative_to(ROOT)}")


# --------------------------------------------------------------------------- #
# S3
# --------------------------------------------------------------------------- #
def ensure_bucket(s3, bucket: str, region: str) -> None:
    try:
        s3.head_bucket(Bucket=bucket)
        log(f"bucket s3://{bucket} already exists")
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code not in ("404", "NoSuchBucket", "403"):
            raise
        if code == "403":
            raise SystemExit(
                f"bucket name '{bucket}' is taken by another AWS account. "
                f"S3 bucket names are globally unique - pick a different S3_BUCKET in .env."
            )
        log(f"creating bucket s3://{bucket} in {region}")
        kwargs = {"Bucket": bucket}
        # us-east-1 is the one region that rejects an explicit location constraint.
        if region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        s3.create_bucket(**kwargs)
        s3.get_waiter("bucket_exists").wait(Bucket=bucket)

    log("enabling versioning (this is what gives free point-in-time history)")
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})

    log("blocking all public access")
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )

    # Versioning without a lifecycle rule is how a free-tier bucket quietly
    # grows forever. 30 days of history is plenty for this project.
    log("applying lifecycle rules (prune old versions and old backups)")
    s3.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "expire-noncurrent-record-versions",
                    "Filter": {"Prefix": "records/"},
                    "Status": "Enabled",
                    "NoncurrentVersionExpiration": {"NoncurrentDays": 30},
                },
                {
                    "ID": "expire-old-backups",
                    "Filter": {"Prefix": "backups/"},
                    "Status": "Enabled",
                    "Expiration": {"Days": 30},
                    "NoncurrentVersionExpiration": {"NoncurrentDays": 7},
                },
                {
                    "ID": "abort-incomplete-uploads",
                    "Filter": {"Prefix": ""},
                    "Status": "Enabled",
                    "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
                },
            ]
        },
    )
    s3.put_bucket_tagging(Bucket=bucket, Tagging={"TagSet": TAGS})


# --------------------------------------------------------------------------- #
# IAM
# --------------------------------------------------------------------------- #
def ensure_instance_profile(iam, bucket: str) -> str:
    """An instance role, not access keys.

    The alternative - putting an access key in the .env on the box - is the
    single most common way small AWS projects leak credentials. The role gives
    the instance temporary, rotating credentials scoped to this one bucket.
    """
    assume_policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "ec2.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    }
    try:
        iam.get_role(RoleName=ROLE_NAME)
        log(f"IAM role {ROLE_NAME} already exists")
    except iam.exceptions.NoSuchEntityException:
        log(f"creating IAM role {ROLE_NAME}")
        iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(assume_policy),
            Description="ZeroDownPipeline EC2 instance: S3 access for records and backups",
            Tags=TAGS,
        )

    bucket_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ListOwnBucket",
                "Effect": "Allow",
                "Action": ["s3:ListBucket", "s3:GetBucketLocation", "s3:ListBucketVersions"],
                "Resource": f"arn:aws:s3:::{bucket}",
            },
            {
                "Sid": "ReadWriteObjects",
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject",
                           "s3:GetObjectVersion"],
                "Resource": f"arn:aws:s3:::{bucket}/*",
            },
        ],
    }
    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="zerodownpipeline-s3-access",
        PolicyDocument=json.dumps(bucket_policy),
    )
    log("attached inline S3 policy scoped to this bucket only")

    try:
        iam.get_instance_profile(InstanceProfileName=PROFILE_NAME)
        log(f"instance profile {PROFILE_NAME} already exists")
    except iam.exceptions.NoSuchEntityException:
        log(f"creating instance profile {PROFILE_NAME}")
        iam.create_instance_profile(InstanceProfileName=PROFILE_NAME, Tags=TAGS)
        # IAM is eventually consistent; a brand new profile is not immediately
        # usable by RunInstances. This sleep is not superstition.
        time.sleep(10)

    profile = iam.get_instance_profile(InstanceProfileName=PROFILE_NAME)["InstanceProfile"]
    if not any(r["RoleName"] == ROLE_NAME for r in profile["Roles"]):
        iam.add_role_to_instance_profile(InstanceProfileName=PROFILE_NAME, RoleName=ROLE_NAME)
        log("added role to instance profile")
        time.sleep(10)

    return PROFILE_NAME


# --------------------------------------------------------------------------- #
# EC2
# --------------------------------------------------------------------------- #
def ensure_key_pair(ec2, key_name: str) -> Path:
    key_path = ROOT / f"{key_name}.pem"
    existing = ec2.describe_key_pairs(
        Filters=[{"Name": "key-name", "Values": [key_name]}]
    )["KeyPairs"]

    if existing:
        if not key_path.exists():
            raise SystemExit(
                f"key pair {key_name} exists in AWS but {key_path.name} is missing here.\n"
                "You cannot re-download a private key. Either restore the .pem, or delete\n"
                "the key pair in the EC2 console and re-run this to generate a fresh one."
            )
        log(f"key pair {key_name} already exists")
        return key_path

    log(f"creating key pair {key_name}")
    resp = ec2.create_key_pair(
        KeyName=key_name,
        KeyType="rsa",
        KeyFormat="pem",
        TagSpecifications=[{"ResourceType": "key-pair", "Tags": TAGS}],
    )
    key_path.write_text(resp["KeyMaterial"], encoding="utf-8")
    # SSH refuses to use a world-readable key. This is a no-op on Windows, where
    # the equivalent is an icacls call - the README covers it.
    try:
        key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    log(f"private key written to {key_path.name} - it is gitignored, keep it safe, "
        f"AWS will not give you another copy")
    return key_path


def default_vpc(ec2) -> str:
    vpcs = ec2.describe_vpcs(Filters=[{"Name": "isDefault", "Values": ["true"]}])["Vpcs"]
    if not vpcs:
        raise SystemExit("no default VPC in this region - create one, or set a VPC id by hand")
    return vpcs[0]["VpcId"]


def ensure_security_group(ec2, vpc_id: str, my_ip: str) -> str:
    groups = ec2.describe_security_groups(Filters=[
        {"Name": "group-name", "Values": [SG_NAME]},
        {"Name": "vpc-id", "Values": [vpc_id]},
    ])["SecurityGroups"]

    if groups:
        sg_id = groups[0]["GroupId"]
        log(f"security group {SG_NAME} already exists ({sg_id})")
    else:
        log(f"creating security group {SG_NAME}")
        sg_id = ec2.create_security_group(
            GroupName=SG_NAME,
            Description="ZeroDownPipeline: SSH from admin IP, HTTP from anywhere",
            VpcId=vpc_id,
            TagSpecifications=[{"ResourceType": "security-group", "Tags": TAGS}],
        )["GroupId"]

    ssh_cidr = "0.0.0.0/0" if my_ip == "0.0.0.0" else f"{my_ip}/32"
    # 8000/8001 are deliberately absent: the app containers publish on
    # 127.0.0.1 and only nginx talks to them, so they never need a rule.
    rules = [
        {
            "IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
            "IpRanges": [{"CidrIp": ssh_cidr, "Description": "SSH from admin"}],
        },
        {
            "IpProtocol": "tcp", "FromPort": 80, "ToPort": 80,
            "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "HTTP"}],
        },
    ]
    for rule in rules:
        try:
            ec2.authorize_security_group_ingress(GroupId=sg_id, IpPermissions=[rule])
            log(f"opened tcp/{rule['FromPort']} to {rule['IpRanges'][0]['CidrIp']}")
        except ClientError as exc:
            if exc.response["Error"]["Code"] == "InvalidPermission.Duplicate":
                log(f"tcp/{rule['FromPort']} rule already present")
            else:
                raise
    return sg_id


def find_running_instance(ec2):
    reservations = ec2.describe_instances(Filters=[
        {"Name": "tag:Name", "Values": [INSTANCE_NAME]},
        {"Name": "instance-state-name", "Values": ["pending", "running"]},
    ])["Reservations"]
    for res in reservations:
        for inst in res["Instances"]:
            return inst
    return None


def launch_instance(ec2, ssm, key_name: str, sg_id: str, profile_name: str,
                    instance_type: str, env: dict) -> dict:
    existing = find_running_instance(ec2)
    if existing:
        log(f"instance {existing['InstanceId']} is already running - not launching another")
        instance_id = existing["InstanceId"]
    else:
        ami_id = ssm.get_parameter(Name=AL2023_SSM_PARAM)["Parameter"]["Value"]
        log(f"latest Amazon Linux 2023 AMI in this region: {ami_id}")
        log(f"launching {instance_type} instance")
        resp = ec2.run_instances(
            ImageId=ami_id,
            InstanceType=instance_type,
            KeyName=key_name,
            MaxCount=1, MinCount=1,
            SecurityGroupIds=[sg_id],
            IamInstanceProfile={"Name": profile_name},
            UserData=USER_DATA.read_text(encoding="utf-8"),
            BlockDeviceMappings=[{
                "DeviceName": "/dev/xvda",
                # 30 GB is the free-tier EBS ceiling; MySQL x2 plus images
                # outgrows the 8 GB default surprisingly fast.
                "Ebs": {"VolumeSize": 30, "VolumeType": "gp3", "DeleteOnTermination": True},
            }],
            MetadataOptions={"HttpTokens": "required"},  # IMDSv2 only
            TagSpecifications=[
                {"ResourceType": "instance",
                 "Tags": TAGS + [{"Key": "Name", "Value": INSTANCE_NAME}]},
                {"ResourceType": "volume", "Tags": TAGS},
            ],
        )
        instance_id = resp["Instances"][0]["InstanceId"]
        log(f"launched {instance_id}")

    log("waiting for the instance to reach 'running'")
    ec2.get_waiter("instance_running").wait(InstanceIds=[instance_id])
    log("waiting for status checks to pass (this is usually the slow part)")
    ec2.get_waiter("instance_status_ok").wait(InstanceIds=[instance_id])

    inst = ec2.describe_instances(InstanceIds=[instance_id])["Reservations"][0]["Instances"][0]
    return {
        "instance_id": instance_id,
        "public_ip": inst.get("PublicIpAddress", ""),
        "public_dns": inst.get("PublicDnsName", ""),
        "private_ip": inst.get("PrivateIpAddress", ""),
        "instance_type": inst["InstanceType"],
        "availability_zone": inst["Placement"]["AvailabilityZone"],
    }


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="Provision ZeroDownPipeline AWS infrastructure")
    parser.add_argument("--skip-instance", action="store_true",
                        help="create S3/IAM/SG/key only, do not launch EC2")
    args = parser.parse_args()

    env = load_env()
    region = env.get("AWS_REGION") or "ap-south-1"
    bucket = env.get("S3_BUCKET") or ""
    key_name = env.get("EC2_KEY_NAME") or "zerodownpipeline-key"
    instance_type = env.get("EC2_INSTANCE_TYPE") or "t3.micro"

    if not bucket or "yourname" in bucket:
        raise SystemExit(
            "set S3_BUCKET in .env to something globally unique, e.g. "
            "zerodownpipeline-data-harsha2515"
        )
    if not USER_DATA.exists():
        raise SystemExit(f"missing {USER_DATA}")

    session = boto3.Session(
        region_name=region,
        profile_name=env.get("AWS_PROFILE") or None,
    )
    identity = session.client("sts").get_caller_identity()
    log(f"account {identity['Account']} as {identity['Arn'].split('/')[-1]} in {region}")

    s3 = session.client("s3")
    iam = session.client("iam")
    ec2 = session.client("ec2")
    ssm = session.client("ssm")

    ensure_bucket(s3, bucket, region)
    profile_name = ensure_instance_profile(iam, bucket)
    key_path = ensure_key_pair(ec2, key_name)
    vpc_id = default_vpc(ec2)
    my_ip = my_public_ip()
    log(f"your public IP looks like {my_ip}")
    sg_id = ensure_security_group(ec2, vpc_id, my_ip)

    outputs = {
        "region": region,
        "account_id": identity["Account"],
        "s3_bucket": bucket,
        "vpc_id": vpc_id,
        "security_group_id": sg_id,
        "key_name": key_name,
        "key_path": str(key_path.relative_to(ROOT)),
        "instance_profile": profile_name,
    }

    if args.skip_instance:
        log("--skip-instance given: stopping before EC2")
    else:
        outputs.update(launch_instance(ec2, ssm, key_name, sg_id, profile_name, instance_type, env))

    write_outputs(outputs)

    print()
    log("done. Summary:")
    for key, value in outputs.items():
        print(f"    {key:20} {value}")

    if outputs.get("public_ip"):
        print()
        log("next steps:")
        print(f"    ssh -i {key_path.name} ec2-user@{outputs['public_ip']}")
        print("    python deploy/sync_scripts.py       # copy scripts + .env to the box")
        print("    (on the box) /opt/zerodown/scripts/db/bootstrap_db.sh")
        print()
        log("first boot installs Docker, nginx and the AWS CLI - give it 2-3 minutes.")
        log("watch it with: sudo tail -f /var/log/user-data.log")
    return 0


if __name__ == "__main__":
    sys.exit(main())
