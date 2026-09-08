"""s3_store must degrade quietly - an S3 outage cannot fail a user request."""
from botocore.exceptions import ClientError

from app import config, s3_store


class BoomClient:
    def put_object(self, **kwargs):
        raise ClientError({"Error": {"Code": "AccessDenied"}}, "PutObject")

    def get_object(self, **kwargs):
        raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")


def test_disabled_when_no_bucket(monkeypatch):
    monkeypatch.setattr(config, "S3_BUCKET", "")
    assert s3_store.enabled() is False
    assert s3_store.put_record("abc", "https://example.com") is False


def test_put_record_swallows_client_error(monkeypatch):
    monkeypatch.setattr(config, "S3_BUCKET", "some-bucket")
    monkeypatch.setattr(s3_store, "_client", lambda: BoomClient())
    assert s3_store.put_record("abc", "https://example.com") is False


def test_get_record_swallows_client_error(monkeypatch):
    monkeypatch.setattr(config, "S3_BUCKET", "some-bucket")
    monkeypatch.setattr(s3_store, "_client", lambda: BoomClient())
    assert s3_store.get_record("abc") is None


def test_put_record_success(monkeypatch):
    captured = {}

    class OKClient:
        def put_object(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(config, "S3_BUCKET", "some-bucket")
    monkeypatch.setattr(s3_store, "_client", lambda: OKClient())
    assert s3_store.put_record("abc", "https://example.com") is True
    assert captured["Key"] == "records/abc.json"
