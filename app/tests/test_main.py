def test_health_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "ok"
    assert "version" in body
    assert body["checks"]["mysql_primary"] is True


def test_health_fails_when_primary_down(client, fake_db):
    fake_db.up = False
    resp = client.get("/health")
    assert resp.status_code == 503
    assert resp.get_json()["status"] == "error"


def test_health_break_switch(client, monkeypatch):
    """The rollback demo depends on this flag actually failing the check."""
    monkeypatch.setenv("BREAK_HEALTHCHECK", "1")
    resp = client.get("/health")
    assert resp.status_code == 500


def test_shorten_returns_code(client):
    resp = client.post("/shorten", json={"url": "https://example.com/a/very/long/path"})
    assert resp.status_code == 201
    body = resp.get_json()
    assert len(body["short_code"]) == 7
    assert body["short_url"].endswith(body["short_code"])


def test_shorten_rejects_bad_url(client):
    for bad in ["", "not-a-url", "ftp://example.com", "javascript:alert(1)"]:
        resp = client.post("/shorten", json={"url": bad})
        assert resp.status_code == 400, bad


def test_shorten_rejects_missing_body(client):
    resp = client.post("/shorten", data="", content_type="application/json")
    assert resp.status_code == 400


def test_redirect_and_click_count(client, fake_db):
    code = client.post("/shorten", json={"url": "https://example.com"}).get_json()["short_code"]

    resp = client.get(f"/{code}", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"] == "https://example.com"

    client.get(f"/{code}")
    assert fake_db.rows[code]["clicks"] == 2


def test_redirect_unknown_code_is_404(client):
    assert client.get("/doesnotexist").status_code == 404


def test_stats(client):
    code = client.post("/shorten", json={"url": "https://example.com"}).get_json()["short_code"]
    client.get(f"/{code}")

    body = client.get(f"/stats/{code}").get_json()
    assert body["original_url"] == "https://example.com"
    assert body["clicks"] == 1
    assert body["created_at"] is not None


def test_stats_unknown_code_is_404(client):
    assert client.get("/stats/nope").status_code == 404


def test_redirect_served_from_cache_when_db_row_missing(client, fake_cache, fake_db):
    """Cache hit must not require a database read."""
    fake_cache["cached1"] = "https://cached.example.com"
    resp = client.get("/cached1", follow_redirects=False)
    assert resp.status_code == 302
    assert resp.headers["Location"] == "https://cached.example.com"


def test_index_reports_version(client):
    body = client.get("/").get_json()
    assert body["service"] == "zerodownpipeline-api"
    assert "version" in body
