import json
import os

import pytest

from app import config, settings


@pytest.fixture
def clean(tmp_path, monkeypatch):
    """Isolated settings file; restore config/env afterwards."""
    monkeypatch.setenv("RV_SETTINGS_FILE", str(tmp_path / "settings.json"))
    monkeypatch.setenv("RV_SETTINGS_EDITABLE", "true")
    saved = {f.attr: getattr(config, f.attr) for f in settings.FIELDS if f.attr}
    saved_env = {f.env: os.environ.get(f.env) for f in settings.FIELDS if not f.attr}
    settings._overrides.clear()
    yield tmp_path / "settings.json"
    settings._overrides.clear()
    for a, v in saved.items():
        setattr(config, a, v)
    for e, v in saved_env.items():
        os.environ.pop(e, None) if v is None else os.environ.__setitem__(e, v)


def post(client, values=None, clear=None, **kw):
    return client.post("/api/settings", json={"values": values or {}, "clear": clear or []}, **kw)


def test_page_has_version_footer_and_settings(client, clean):
    from app.version import VERSION
    for path in ("/", "/rv", "/guide", "/settings"):
        r = client.get(path)
        assert r.status_code == 200 and f"RV Analyzer v{VERSION}" in r.text, path
    r = client.get("/settings")
    assert "Test AI connection" in r.text and "Save settings" in r.text and "rv_component_up" in r.text


def test_save_applies_immediately_and_persists(client, clean):
    r = post(client, {"ai_base_url": "http://10.0.0.5:3000/", "ai_model": "llama3.1:8b", "ai_backend": "ollama",
                      "judging_options": "3"})
    assert r.status_code == 200, r.text
    assert set(r.json()["changed"]) == {"ai_base_url", "ai_model", "ai_backend", "judging_options"}
    assert config.AI_BASE_URL == "http://10.0.0.5:3000" and config.AI_MODEL == "llama3.1:8b"
    assert config.AI_BACKEND == "ollama" and config.JUDGING_OPTIONS == 3
    from app import ai_feedback
    assert ai_feedback.enabled()
    data = json.loads(clean.read_text())
    assert data["values"]["ai_model"] == "llama3.1:8b"
    assert oct(clean.stat().st_mode & 0o777) == "0o600"
    # reloading from disk (as after a restart) gives the same effective values
    config.AI_MODEL = ""
    settings.startup()
    assert config.AI_MODEL == "llama3.1:8b"
    view = {f["key"]: f for g in client.get("/api/settings").json()["groups"] for f in g["fields"]}
    assert view["ai_model"]["source"] == "settings"


def test_saving_unchanged_env_values_creates_no_overrides(client, clean):
    cur = {f.key: str(settings.effective(f.key)) for f in settings.FIELDS if not f.secret}
    r = post(client, cur)
    assert r.status_code == 200 and r.json()["changed"] == []
    assert not clean.exists()


def test_clearing_reverts_to_environment_default(client, clean):
    post(client, {"judging_options": "4"})
    assert config.JUDGING_OPTIONS == 4
    r = post(client, {"judging_options": ""})
    assert r.json()["changed"] == ["judging_options"]
    assert config.JUDGING_OPTIONS == settings._BASELINE["judging_options"]


def test_secrets_never_returned_and_blank_keeps(client, clean):
    post(client, {"ai_api_key": "sk-super-secret", "pexels_api_key": "pex-123"})
    assert config.AI_API_KEY == "sk-super-secret"
    body = client.get("/api/settings").text + client.get("/settings").text
    assert "sk-super-secret" not in body and "pex-123" not in body
    view = {f["key"]: f for g in client.get("/api/settings").json()["groups"] for f in g["fields"]}
    assert view["ai_api_key"]["is_set"] is True and view["ai_api_key"]["value"] == ""
    assert post(client, {"ai_api_key": ""}).json()["changed"] == []          # blank = keep
    assert config.AI_API_KEY == "sk-super-secret"
    assert post(client, {}, clear=["ai_api_key"]).json()["changed"] == ["ai_api_key"]
    assert config.AI_API_KEY == settings._BASELINE["ai_api_key"]


@pytest.mark.parametrize("values", [
    {"ai_base_url": "ftp://x"}, {"ai_base_url": "http://user:pw@host"}, {"ai_base_url": "not a url"},
    {"judging_options": "9"}, {"judging_options": "two"}, {"ai_backend": "gpt"}, {"nope": "x"},
    {"ai_model": "bad\x00name"}, {"otlp_endpoint": "javascript:alert(1)"},
])
def test_validation_rejects_and_saves_nothing(client, clean, values):
    before = dict(settings._overrides)
    r = post(client, {**values, "ai_model": values.get("ai_model", "ok-model")})
    assert r.status_code == 400 and r.json()["errors"]
    assert settings._overrides == before and not clean.exists()


def test_read_only_without_auth_or_flag(client, clean, monkeypatch):
    monkeypatch.delenv("RV_SETTINGS_EDITABLE")
    assert client.get("/api/settings").json()["editable"] is False
    assert post(client, {"ai_model": "x"}).status_code == 403
    assert "read-only" in client.get("/settings").text


def test_requires_json_content_type(client, clean):
    r = client.post("/api/settings", content=b'{"values":{"ai_model":"x"}}', headers={"Content-Type": "text/plain"})
    assert r.status_code == 415
    r = client.post("/api/settings", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400
    r = client.post("/api/settings", content=b"x" * 20000, headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_tracing_setting_flags_restart(client, clean):
    settings._STARTUP.update({"otlp_endpoint": "", "otlp_protocol": settings.effective("otlp_protocol")})
    r = post(client, {"otlp_endpoint": "http://192.168.10.78:4318"})
    assert r.json()["restart_pending"] == ["OTLP endpoint"]
    assert os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://192.168.10.78:4318"
    post(client, {"otlp_endpoint": ""})
    assert settings.restart_pending() == []


def test_ai_test_endpoint_when_unconfigured(client, clean):
    r = client.post("/api/settings/test", json={"what": "ai"})
    assert r.status_code == 200 and r.json()["ok"] is False and r.json()["enabled"] is False
    assert client.post("/api/settings/test", json={"what": "x"}).status_code == 400


def test_corrupt_settings_file_is_ignored(client, clean):
    clean.write_text("{ nope")
    settings.load()
    assert settings._overrides == {}
    clean.write_text(json.dumps({"values": {"judging_options": 99, "ai_model": "m", "bogus": 1}}))
    settings.load()
    assert settings._overrides == {"ai_model": "m"}


def test_auth_protects_settings(client, clean, monkeypatch):
    monkeypatch.setattr(config, "AUTH_USER", "u")
    monkeypatch.setattr(config, "AUTH_PASSWORD", "p")
    assert client.get("/settings").status_code == 401
    assert client.post("/api/settings", json={"values": {}}).status_code == 401


def test_full_form_post_changes_only_what_the_user_edited(client, clean):
    form = {f.key: str(settings.effective(f.key)) for f in settings.FIELDS if not f.secret}
    form["otlp_endpoint"] = "http://tempo:4318"
    r = post(client, form)
    assert r.json()["changed"] == ["otlp_endpoint"]
    assert r.json()["restart_pending"] == ["OTLP endpoint"]
