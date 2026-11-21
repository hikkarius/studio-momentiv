"""Version check, «Оновити» button guards and the first-run Gemini key check."""
import json

from fastapi.testclient import TestClient

from backend import app_update, server
import config_loader


client = TestClient(server.app)


def test_is_newer_compares_numbers_not_text():
    assert app_update.is_newer("1.10.0", "1.9.3")
    assert app_update.is_newer("v2.0", "1.99.99")
    assert not app_update.is_newer("1.0.0", "1.0.0")
    assert not app_update.is_newer(None, "1.0.0")
    assert not app_update.is_newer("0.9.9", "1.0.0")


def test_version_file_is_valid():
    info = app_update.read_version()
    assert info["repo"].count("/") == 1
    assert info["asset"].endswith(".zip")
    assert len(app_update._parse(info["version"])) >= 3


def test_app_info_reports_update_and_caches(monkeypatch, tmp_path):
    (tmp_path / "version.json").write_text(json.dumps({"version": "1.0.0", "repo": "a/b", "asset": "x.zip"}), encoding="utf-8")
    monkeypatch.setattr(app_update, "VERSION_FILE", tmp_path / "version.json")
    monkeypatch.setattr(app_update, "_cache", {"at": 0.0, "latest": None, "url": None})
    calls = []
    monkeypatch.setattr(app_update, "_fetch_latest", lambda repo: calls.append(repo) or ("1.2.0", "https://example/rel"))

    data = client.get("/api/app-info").json()
    assert data["version"] == "1.0.0" and data["latest"] == "1.2.0" and data["update_available"] is True
    client.get("/api/app-info")
    assert calls == ["a/b"]  # second call served from cache


def test_app_info_survives_no_internet(monkeypatch):
    monkeypatch.setattr(app_update, "_cache", {"at": 0.0, "latest": None, "url": None})

    def boom(repo):
        raise OSError("no network")

    monkeypatch.setattr(app_update, "_fetch_latest", boom)
    data = client.get("/api/app-info").json()
    assert data["update_available"] is False and data["latest"] is None


def test_update_refused_in_git_checkout(monkeypatch):
    monkeypatch.setattr(app_update, "is_dev_checkout", lambda: True)
    monkeypatch.setattr(app_update, "_fetch_latest", lambda repo: ("9.9.9", None))
    started = []
    monkeypatch.setattr(app_update, "start_update", lambda: started.append(1))
    r = client.post("/api/update")
    assert r.status_code == 400 and "git" in r.json()["detail"]
    assert started == []


def test_check_key_saves_only_a_working_key(monkeypatch, tmp_path):
    env = tmp_path / ".env"
    monkeypatch.setattr(config_loader, "ENV_PATH", env)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    monkeypatch.setattr(app_update, "check_gemini_key", lambda k: {"ok": False, "message": "Google не прийняв цей ключ."})
    r = client.post("/api/settings/check-key", json={"key": "x" * 39})
    assert r.status_code == 200 and r.json()["ok"] is False
    assert not env.exists()

    monkeypatch.setattr(app_update, "check_gemini_key", lambda k: {"ok": True, "message": "Ключ працює."})
    r = client.post("/api/settings/check-key", json={"key": "y" * 39})
    assert r.json()["ok"] is True
    assert env.read_text(encoding="utf-8").strip() == "GEMINI_API_KEY=" + "y" * 39


def test_check_key_rejects_obvious_garbage():
    r = client.post("/api/settings/check-key", json={"key": "short key"})
    assert r.status_code == 400
