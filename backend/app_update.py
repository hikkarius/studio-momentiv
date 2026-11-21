"""
app_update.py — version of this copy of the studio, «є нова версія?» check against GitHub Releases,
starting installer/update.ps1, and a real check of a Gemini key for the first-run screen.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

log = logging.getLogger("shorts_core")

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "version.json"
UPDATE_SCRIPT = ROOT / "installer" / "update.ps1"
_CHECK_TTL = 6 * 3600

_cache: Dict[str, Any] = {"at": 0.0, "latest": None, "url": None}
_cache_lock = threading.Lock()


def read_version() -> Dict[str, str]:
    try:
        data = json.loads(VERSION_FILE.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return {
        "version": str(data.get("version", "0.0.0")),
        "repo": str(data.get("repo", "")),
        "asset": str(data.get("asset", "")),
    }


def _parse(v: str) -> tuple:
    parts = []
    for p in v.strip().lstrip("vV").split("."):
        digits = "".join(ch for ch in p if ch.isdigit())
        parts.append(int(digits) if digits else 0)
    return tuple(parts + [0] * (3 - len(parts)))


def is_newer(latest: Optional[str], current: str) -> bool:
    return bool(latest) and _parse(latest) > _parse(current)


def is_dev_checkout() -> bool:
    """A git working copy (Artur's laptop) is updated with git, never by the updater."""
    return (ROOT / ".git").exists()


def _fetch_latest(repo: str) -> tuple[Optional[str], Optional[str]]:
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/releases/latest",
        headers={"User-Agent": "studio-momentiv", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(req, timeout=4) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return str(data.get("tag_name", "")).lstrip("vV") or None, data.get("html_url")


def app_info(force: bool = False) -> Dict[str, Any]:
    info = read_version()
    latest, url = None, None
    if info["repo"]:
        with _cache_lock:
            fresh = time.time() - _cache["at"] < _CHECK_TTL
            if fresh and not force:
                latest, url = _cache["latest"], _cache["url"]
            else:
                try:
                    latest, url = _fetch_latest(info["repo"])
                except Exception as e:
                    log.debug(f"update check failed: {e}")
                _cache.update(at=time.time(), latest=latest, url=url)
    dev = is_dev_checkout()
    return {
        "version": info["version"],
        "latest": latest,
        "release_url": url,
        "update_available": is_newer(latest, info["version"]),
        "can_self_update": sys.platform == "win32" and UPDATE_SCRIPT.exists() and not dev,
        "dev_checkout": dev,
    }


def start_update() -> None:
    """Opens update.ps1 in its own window; the caller then stops the server so files can be replaced."""
    flags = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(UPDATE_SCRIPT), "-FromStudio"],
        cwd=str(ROOT),
        creationflags=flags,
        close_fds=True,
    )


def exit_soon(delay: float = 1.0) -> None:
    def _bye():
        time.sleep(delay)
        os._exit(0)

    threading.Thread(target=_bye, daemon=True).start()


def check_gemini_key(key: str) -> Dict[str, Any]:
    """Asks Gemini for its model list with this key: free, and tells a wrong key from no internet."""
    try:
        from google import genai
        from google.genai import types

        client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=15000))
        next(iter(client.models.list(config={"page_size": 1})), None)
        return {"ok": True, "message": "Ключ працює."}
    except Exception as e:
        text = str(e)
        low = text.lower()
        if any(s in low for s in ("api_key_invalid", "api key not valid", "invalid api key", "permission_denied", "unauthenticated", "401", "403")):
            msg = "Google не прийняв цей ключ. Скопіюй його з AI Studio ще раз, повністю."
        elif any(s in low for s in ("timed out", "timeout", "connection", "getaddrinfo", "network", "ssl")):
            msg = "Не вдалося зв'язатися з Google. Перевір інтернет і спробуй ще раз."
        elif "429" in low or "resource_exhausted" in low:
            return {"ok": True, "message": "Ключ працює, але ліміт на зараз вичерпано. Спробуй трохи пізніше."}
        else:
            msg = f"Не вдалося перевірити ключ: {text[:160]}"
        log.info(f"Gemini key check failed: {text[:300]}")
        return {"ok": False, "message": msg}
