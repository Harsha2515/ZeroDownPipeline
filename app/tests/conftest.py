"""Test fixtures.

The unit tests must run inside the Jenkins build container, where there is no
MySQL, no Redis and no AWS. So the two I/O modules are replaced with in-memory
fakes. This keeps stage 3 of the pipeline fast and hermetic - the real
dependencies are exercised later by the health check against a live container.
"""
from datetime import datetime, timezone

import pytest

from app import cache, db, main, s3_store


class FakeDB:
    def __init__(self):
        self.rows = {}
        self.up = True

    def insert_link(self, short_code, original_url):
        if short_code in self.rows:
            raise RuntimeError("duplicate key")
        self.rows[short_code] = {
            "short_code": short_code,
            "original_url": original_url,
            "clicks": 0,
            "created_at": datetime.now(timezone.utc),
        }

    def get_link(self, short_code, use_replica=False):
        return self.rows.get(short_code)

    def increment_clicks(self, short_code):
        if short_code in self.rows:
            self.rows[short_code]["clicks"] += 1

    def ping(self, which="primary"):
        return self.up


@pytest.fixture
def fake_db(monkeypatch):
    fake = FakeDB()
    for name in ("insert_link", "get_link", "increment_clicks", "ping"):
        monkeypatch.setattr(db, name, getattr(fake, name))
    return fake


@pytest.fixture
def fake_cache(monkeypatch):
    store = {}
    monkeypatch.setattr(cache, "get_url", lambda code: store.get(code))
    monkeypatch.setattr(cache, "set_url", lambda code, url: store.__setitem__(code, url))
    monkeypatch.setattr(cache, "invalidate", lambda code: store.pop(code, None))
    monkeypatch.setattr(cache, "ping", lambda: True)
    return store


@pytest.fixture
def no_s3(monkeypatch):
    monkeypatch.setattr(s3_store, "put_record", lambda *a, **k: True)
    monkeypatch.setattr(s3_store, "get_record", lambda *a, **k: None)


@pytest.fixture
def client(fake_db, fake_cache, no_s3, monkeypatch):
    monkeypatch.delenv("BREAK_HEALTHCHECK", raising=False)
    main.app.config["TESTING"] = True
    with main.app.test_client() as c:
        yield c
