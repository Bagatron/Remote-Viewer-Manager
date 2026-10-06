"""Optional HTTP Basic auth."""
import base64

import pytest

from app import config


def hdr(user, pw):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


@pytest.fixture()
def locked(monkeypatch):
    monkeypatch.setattr(config, "AUTH_USER", "mickey")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "s3cret: with colon")


def test_open_by_default(client):
    assert client.get("/rv").status_code == 200


def test_requires_login_when_configured(client, locked):
    r = client.get("/rv")
    assert r.status_code == 401 and "Basic" in r.headers["www-authenticate"]
    assert client.get("/api/sessions").status_code == 401
    assert client.post("/rv/start", follow_redirects=False).status_code == 401


def test_wrong_and_right_credentials(client, locked):
    assert client.get("/rv", headers=hdr("mickey", "nope")).status_code == 401
    assert client.get("/rv", headers=hdr("other", "s3cret: with colon")).status_code == 401
    assert client.get("/rv", headers={"Authorization": "Basic !!!notbase64"}).status_code == 401
    assert client.get("/rv", headers=hdr("mickey", "s3cret: with colon")).status_code == 200  # ':' in password ok


def test_probes_and_metrics_stay_open(client, locked, monkeypatch):
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200
    assert client.get("/metrics").status_code == 200
    monkeypatch.setattr(config, "AUTH_PROTECT_METRICS", True)
    assert client.get("/metrics").status_code == 401


def test_guide_page(client):
    r = client.get("/guide")
    assert r.status_code == 200
    for needle in ("What we are going for", "RV index", "Intuitive window", "Hit rate", "50%"):
        assert needle in r.text
    assert 'href="/guide"' in client.get("/rv").text      # linked from the header on every page
