"""
«Що зараз доступно твоєму ключу» (Налаштування → Ключ і розпізнавання): one tiny request to each
Gemini model the studio may use, so the person sees which ones answer now, which are busy at Google,
which are out of today's limit and which this key can only use with billing.

The last result is kept in gemini_models_check.json (shown again after a restart, not data to keep).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger(__name__)

CHECK_PATH = Path(__file__).resolve().parent.parent / "gemini_models_check.json"
_PROBE_TIMEOUT_MS = 60_000
_lock = threading.Lock()

# What the card says for each status
STATUS_TEXT = {
    "ok": "працює",
    "busy": "безкоштовна, але зараз перевантажена в Google",
    "limit": "ліміт на сьогодні вичерпано, оновиться після 10:00 за Києвом",
    "minute": "забагато запитів за хвилину, спробуй трохи пізніше",
    "paid": "лише з платним ключем",
    "missing": "Google більше не має такої моделі",
    "bad_key": "Google не прийняв ключ",
    "offline": "немає зв'язку з Google",
    "error": "не відповіла",
}


def pretty_name(model: str) -> str:
    """gemini-4-hydrogen → Gemini 4 Hydrogen, gemini-3.5-flash-lite → Gemini 3.5 Flash-Lite."""
    m = re.match(r"^gemini-([0-9.]+)-(.+)$", model or "")
    if not m:
        return model
    rest = m.group(2).replace("flash-lite", "Flash‑Lite").replace("-", " ")
    rest = " ".join(w if w[:1].isupper() else w.capitalize() for w in rest.split())
    return f"Gemini {m.group(1)} {rest}".replace("‑", "-")


def models_to_check() -> List[Dict[str, str]]:
    """The models the studio may ask, each once, with its role."""
    from config_loader import app_config

    g = app_config.gemini
    out: List[Dict[str, str]] = []
    seen = set()
    chosen = str(getattr(g, "experimental_model", "auto") or "auto")
    extra = [chosen] if chosen not in ("auto", "fast") else []
    for role, names in (("strong", list(getattr(g, "strong_models", []) or []) + extra),
                        ("fast", list(g.candidate_models)),
                        ("fallback", list(getattr(g, "fallback_models", []) or []))):
        for m in names:
            if m and m not in seen:
                seen.add(m)
                out.append({"model": m, "role": role})
    return out


def classify(err: str) -> str:
    low = err.lower()
    if any(s in low for s in ("api_key_invalid", "api key not valid", "api key expired", "permission_denied",
                              "unauthenticated")):
        return "bad_key"
    if "404" in low or "not_found" in low or "is not found" in low:
        return "missing"
    if "429" in low or "resource_exhausted" in low or "quota" in low:
        if re.search(r"limit:\s*0(?![\d.])", err):
            return "paid"
        if "perday" in low or "per_day" in low or "per day" in low:
            return "limit"
        return "minute"
    if any(s in low for s in ("503", "unavailable", "high demand", "overloaded", "504", "deadline", "timed out",
                              "timeout", "500", "internal")):
        return "busy"
    if any(s in low for s in ("getaddrinfo", "connection", "network", "ssl")):
        return "offline"
    return "error"


def _probe(client, model: str) -> Dict[str, Any]:
    from google.genai import types

    started = time.monotonic()
    try:
        client.models.generate_content(
            model=model, contents="Відповідай одним словом: так",
            config=types.GenerateContentConfig(
                max_output_tokens=256,
                automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                http_options=types.HttpOptions(timeout=_PROBE_TIMEOUT_MS),
            ))
        return {"status": "ok", "seconds": round(time.monotonic() - started, 1)}
    except Exception as e:
        err = str(e)
        status = classify(err)
        log.info(f"Gemini model check: {model} → {status} ({err[:200]})")
        if status in ("limit", "paid"):
            try:
                from ai_selector import mark_exhausted_today
                mark_exhausted_today(model)
            except Exception as me:  # the check itself must not fail because of the note
                log.warning(f"Could not remember the limit of {model}: {me}")
        return {"status": status}


def check_models(client=None) -> Dict[str, Any]:
    """Asks every model at once (in parallel) and saves the result."""
    if client is None:
        from config_loader import get_gemini_client
        client = get_gemini_client()
    items = models_to_check()
    with ThreadPoolExecutor(max_workers=max(1, min(6, len(items)))) as pool:
        results = list(pool.map(lambda it: _probe(client, it["model"]), items))
    rows = [{**it, **res, "name": pretty_name(it["model"]), "text": STATUS_TEXT[res["status"]]}
            for it, res in zip(items, results)]
    data = {"checked_at": time.time(), "models": rows}
    with _lock:
        try:
            tmp = CHECK_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(CHECK_PATH)
        except OSError as e:
            log.warning(f"Could not save the model check: {e}")
    return data


def last_check() -> Optional[Dict[str, Any]]:
    with _lock:
        try:
            data = json.loads(CHECK_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
    return data if isinstance(data, dict) and isinstance(data.get("models"), list) else None
