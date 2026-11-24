"""
tests/test_config_loader.py — Unit tests for config_loader.py.
"""
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import config_loader
from config_loader import _load_app_config, ensure_gemini_api_key, get_gemini_client


def test_ensure_gemini_api_key_from_environ(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIzaTestKeyFromEnv123")
    key = ensure_gemini_api_key()
    assert key == "AIzaTestKeyFromEnv123"


def test_ensure_gemini_api_key_from_env_file(tmp_path, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    fake_env = tmp_path / ".env"
    fake_key = "AIza" + "TestKeyFromFile456"
    fake_env.write_text(f'GEMINI_API_KEY="{fake_key}"\n', encoding="utf-8")
    monkeypatch.setattr(config_loader, "ENV_PATH", fake_env)
    assert ensure_gemini_api_key() == fake_key


def test_ensure_gemini_api_key_not_found(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(config_loader, "ENV_PATH", Path("/nonexistent/.env"))
    assert ensure_gemini_api_key() is None


def test_get_gemini_client():
    with patch("state.app_state.get_gemini_client", return_value="mock_genai_client") as mock_get:
        client = get_gemini_client("test_key")
        assert client == "mock_genai_client"
        mock_get.assert_called_once_with(api_key="test_key")


def test_load_app_config_valid():
    cfg = _load_app_config()
    assert cfg.whisper.language == "uk"
    assert cfg.clips.interval_sec == 28


def test_load_app_config_invalid(tmp_path, monkeypatch):
    bad_cfg = tmp_path / "bad_config.yaml"
    bad_cfg.write_text("whisper:\n  cpu_threads: 999\n", encoding="utf-8")
    monkeypatch.setattr(config_loader, "_CFG_PATH", bad_cfg)

    with pytest.raises(ValueError) as exc:
        _load_app_config()
    assert "Невалідний файл налаштувань" in str(exc.value)
