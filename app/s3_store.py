"""Versioned audit copy of each record in S3.

MySQL is the source of truth. S3 gets a JSON copy of every link on creation,
in a bucket with versioning enabled, which gives point-in-time history and a
recovery path for free. Writes are best-effort and never block the request.
"""
import json
import logging
from datetime import datetime, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from . import config

log = logging.getLogger(__name__)

_s3 = None


def _client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=config.AWS_REGION)
    return _s3


def enabled() -> bool:
    return bool(config.S3_BUCKET)


def put_record(short_code: str, original_url: str) -> bool:
    if not enabled():
        return False
    body = {
        "short_code": short_code,
        "original_url": original_url,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "written_by_version": config.APP_VERSION,
    }
    try:
        _client().put_object(
            Bucket=config.S3_BUCKET,
            Key=f"records/{short_code}.json",
            Body=json.dumps(body).encode(),
            ContentType="application/json",
        )
        return True
    except (BotoCoreError, ClientError) as exc:
        log.warning("s3 put_record failed for %s: %s", short_code, exc)
        return False


def get_record(short_code: str):
    if not enabled():
        return None
    try:
        obj = _client().get_object(
            Bucket=config.S3_BUCKET, Key=f"records/{short_code}.json"
        )
        return json.loads(obj["Body"].read())
    except (BotoCoreError, ClientError) as exc:
        log.warning("s3 get_record failed for %s: %s", short_code, exc)
        return None
