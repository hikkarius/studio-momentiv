"""
config_loader.py — Configuration loading and Gemini client initialization.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger("shorts_core")

_CFG_PATH = Path(__file__).parent / "config.yaml"
# The Gemini key lives here (tests point it at a temp file, so they never read the real key).
ENV_PATH = Path(__file__).parent / ".env"


def ensure_gemini_api_key() -> str | None:
    """
    Finds and sets GEMINI_API_KEY from environment or .env file.
    Returns the key or None if not found.
    """
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]

    env_file = ENV_PATH
    if env_file.exists():
        try:
            from dotenv import load_dotenv
            load_dotenv(dotenv_path=env_file)
            if os.environ.get("GEMINI_API_KEY"):
                return os.environ["GEMINI_API_KEY"]
        except Exception:
            pass

        # Fallback raw line parser
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("GEMINI_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if key:
                        os.environ["GEMINI_API_KEY"] = key
                        return key
        except Exception:
            pass

    return None


# Eager discovery on module load
ensure_gemini_api_key()


def get_gemini_client(api_key: Optional[str] = None):
    """Initializes and returns a Google GenAI Client via AppState."""
    from state import app_state
    return app_state.get_gemini_client(api_key=api_key)


from config_schema import AppConfig


# Changes made in the studio's «Налаштування» screen. Kept apart from config.yaml so that
# its comments survive and a code update never overwrites the user's choices.
USER_SETTINGS_PATH = Path(__file__).parent / "user_settings.json"


def _deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _read_user_settings() -> dict:
    import json
    try:
        if USER_SETTINGS_PATH.exists():
            data = json.loads(USER_SETTINGS_PATH.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception as e:
        log.warning(f"Не вдалося прочитати {USER_SETTINGS_PATH.name}: {e}")
    return {}


def _read_base_config() -> dict:
    if not _CFG_PATH.exists():
        return {}
    with _CFG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_app_config() -> AppConfig:
    """Loads config.yaml (+ user_settings.json on top) and validates it through Pydantic AppConfig."""
    raw_user: dict = {}
    if _CFG_PATH.exists():
        try:
            with _CFG_PATH.open(encoding="utf-8") as f:
                raw_user = yaml.safe_load(f) or {}
        except Exception as e:
            log.error(f"Помилка читання {_CFG_PATH}: {e}")
            raise

    try:
        base = AppConfig.model_validate(raw_user)
    except Exception as e:
        log.error(f"Помилка валідації конфігурації у {_CFG_PATH}:\n{e}")
        raise ValueError(f"Невалідний файл налаштувань {_CFG_PATH}: {e}") from e

    overrides = _read_user_settings()
    if overrides:
        try:
            return AppConfig.model_validate(_deep_merge(raw_user, overrides))
        except Exception as e:
            # A broken user_settings.json must not stop the studio from starting
            log.error(f"Налаштування з {USER_SETTINGS_PATH.name} пропущено: {e}")
    return base


load_config = _load_app_config
app_config: AppConfig = _load_app_config()
CONFIG: dict = app_config.model_dump()
CLIP_INTERVAL_SEC: int = app_config.clips.interval_sec
CANDIDATE_MODELS: list[str] = list(app_config.gemini.candidate_models)


def _assign_in_place(target: AppConfig, source: AppConfig) -> None:
    """Copies values into the live config objects, so modules that imported them see the change."""
    for section in type(source).model_fields:
        tgt_sec = getattr(target, section)
        src_sec = getattr(source, section)
        for field in type(src_sec).model_fields:
            setattr(tgt_sec, field, getattr(src_sec, field))
    CONFIG.clear()
    CONFIG.update(target.model_dump())


def save_user_settings(patch: dict) -> AppConfig:
    """
    Validates and applies a partial settings change ({"clips": {"default_max_len": 45}, ...}),
    stores it in user_settings.json and updates the running config. Raises ValueError if invalid.
    """
    import json
    overrides = _deep_merge(_read_user_settings(), patch)
    try:
        merged = AppConfig.model_validate(_deep_merge(_read_base_config(), overrides))
    except Exception as e:
        raise ValueError(str(e)) from e
    USER_SETTINGS_PATH.write_text(json.dumps(overrides, ensure_ascii=False, indent=2), encoding="utf-8")
    _assign_in_place(app_config, merged)
    return app_config


def reset_user_settings() -> AppConfig:
    """Forgets all studio-screen changes and goes back to config.yaml."""
    try:
        USER_SETTINGS_PATH.unlink()
    except FileNotFoundError:
        pass
    _assign_in_place(app_config, AppConfig.model_validate(_read_base_config()))
    return app_config


def save_gemini_api_key(key: str) -> None:
    """Writes GEMINI_API_KEY into .env (keeping its other lines) and into this process."""
    key = key.strip()
    lines = []
    if ENV_PATH.exists():
        lines = [ln for ln in ENV_PATH.read_text(encoding="utf-8").splitlines()
                 if not ln.strip().startswith("GEMINI_API_KEY=")]
    lines.append(f"GEMINI_API_KEY={key}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ["GEMINI_API_KEY"] = key
