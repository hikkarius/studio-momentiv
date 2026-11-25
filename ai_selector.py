"""
ai_selector.py — Gemini AI clip selection, prompt engineering, context chunking, and boundary validation.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from config_loader import CANDIDATE_MODELS, app_config, get_gemini_client
from clip_refiner import rubric_score
from constants import FILTER_INSTRUCTIONS, FilterMode, resolve_filter_mode
from languages import language_name

log = logging.getLogger("shorts_core")

# Safe character ceiling for transcript content inside a single Gemini prompt
# ~100,000 characters corresponds to ~20,000-25,000 spoken words (~2 hours of speech).
_MAX_TRANSCRIPT_CHARS: int = 100_000

# «Експериментальний» search: the whole transcript goes into one prompt (current Gemini models take
# ~1M tokens, ~400k characters is about 4 hours of speech). Longer videos use overlapping windows.
EXPERIMENTAL_MAX_CHARS: int = 400_000
EXPERIMENTAL_OVERLAP_SEC: float = 120.0
# More clips than this in one answer makes the model output long and unreliable
EXPERIMENTAL_MAX_CLIPS: int = 40

SEARCH_CLASSIC = "classic"
SEARCH_EXPERIMENTAL = "experimental"
SEARCH_MODES = (SEARCH_CLASSIC, SEARCH_EXPERIMENTAL)


def resolve_search_mode(value: Optional[str]) -> str:
    v = (value or "").strip().lower()
    return v if v in SEARCH_MODES else SEARCH_CLASSIC


def validate_clips(
    clips: list[dict],
    video_duration: float,
    min_len: int = 15,
    max_len: int = 60,
    tolerance: float = 3.0,
) -> list[dict]:
    """
    Validates and fixes clip boundaries.

    - Swaps inverted start/end
    - Clamps end to video duration
    - Drops clips shorter than (min_len - tolerance) seconds (never below 5 s)
    - Trims clips longer than (max_len + tolerance) seconds down to max_len
    """
    min_allowed = max(5.0, float(min_len) - tolerance)
    max_allowed = float(max_len) + tolerance

    valid: list[dict] = []
    for i, clip in enumerate(clips, 1):
        try:
            start = float(clip.get("start", 0))
            end = float(clip.get("end", 0))
        except (TypeError, ValueError):
            log.warning(f"Clip {i}: non-numeric bounds — skipping")
            continue

        if start > end:
            log.warning(f"Clip {i}: inverted bounds ({start}s > {end}s) — swapping")
            start, end = end, start

        start = max(0.0, min(start, video_duration - 1))
        end = min(end, video_duration)
        duration = end - start

        if duration < min_allowed:
            log.warning(f"Clip {i}: too short ({duration:.1f}s < {min_allowed:.0f}s) — skipping")
            continue

        if duration > max_allowed:
            log.warning(f"Clip {i}: too long ({duration:.1f}s > {max_allowed:.0f}s) — trimming to {max_len}s")
            end = start + float(max_len)

        raw_score = clip.get("score") if clip.get("score") is not None else clip.get("virality_score")
        parsed_score = 88 - i
        rubric = rubric_score(clip)
        if rubric is not None:
            parsed_score = rubric
        elif isinstance(raw_score, (int, float)):
            parsed_score = int(raw_score)
        elif isinstance(raw_score, str):
            m = re.search(r"\d+", raw_score)
            if m:
                parsed_score = int(m.group(0))

        valid.append({
            **clip,
            "start": start,
            "end": end,
            "score": parsed_score,
            "reason": str(clip.get("reason") or "Сильний хук та висока динаміка мовлення."),
            "caption": str(clip.get("caption") or f"Дивіться у цьому фрагменті: {clip.get('title', f'Кліп #{i}')}"),
            "hashtags": clip.get("hashtags") if isinstance(clip.get("hashtags"), list) and clip.get("hashtags") else ["#shorts", "#тренди", "#відео", "#рекомендації"],
        })

    log.info(f"Clip validation: {len(clips)} in → {len(valid)} valid")
    return valid


def _parse_clips_json(raw_text: str) -> list[dict]:
    """
    Multi-strategy parser to reliably extract JSON clips from model responses.
    Handles Markdown code fences, raw arrays, and wrapped dictionaries.
    """
    clean_text = raw_text.strip()
    if "```" in clean_text:
        clean_text = re.sub(r"^```(?:json)?\s*", "", clean_text, flags=re.MULTILINE)
        clean_text = re.sub(r"```$", "", clean_text, flags=re.MULTILINE).strip()

    data = None
    try:
        data = json.loads(clean_text)
    except Exception:
        # Strategy 2: regex extract outermost JSON array [...]
        match_arr = re.search(r"(\[\s*\{.*\}\s*\])", clean_text, re.DOTALL)
        if match_arr:
            try:
                data = json.loads(match_arr.group(1))
            except Exception:
                pass

    if data is None:
        # Strategy 3: regex extract outermost dict {...}
        match_dict = re.search(r"(\{.*\})", clean_text, re.DOTALL)
        if match_dict:
            try:
                parsed_dict = json.loads(match_dict.group(1))
                for v in parsed_dict.values():
                    if isinstance(v, list) and v:
                        data = v
                        break
            except Exception:
                pass

    if isinstance(data, dict):
        for v in data.values():
            if isinstance(v, list):
                data = v
                break

    if isinstance(data, list) and data:
        valid_items = [
            item for item in data
            if isinstance(item, dict) and "start" in item and "end" in item
        ]
        if valid_items:
            return valid_items

    raise RuntimeError(f"Не вдалося розпарсити список кліпів із відповіді AI: {raw_text[:200]}")


class GeminiUnavailableError(RuntimeError):
    """Raised when no Gemini model produced a usable answer after all retry passes."""


class GeminiQuotaError(GeminiUnavailableError):
    """Every model answered «limit exhausted». `daily` = the per-day limit (resets at midnight
    Pacific time), otherwise a per-minute one; `retry_after` = seconds Google asked to wait, if any."""

    def __init__(self, message: str, daily: bool = False, retry_after: Optional[float] = None):
        super().__init__(message)
        self.daily = daily
        self.retry_after = retry_after


_RETRY_DELAY_RE = re.compile(r"retry(?:_delay|delay)?['\"]?\s*[:=]?\s*['\"]?(\d+(?:\.\d+)?)s", re.IGNORECASE)


def quota_error_from(errors: list[str]) -> GeminiQuotaError:
    """Builds the quota error from the raw 429 texts: per-day or per-minute, and Google's retry delay."""
    joined = " ".join(errors)
    low = joined.lower()
    daily = "perday" in low or "per_day" in low or "per day" in low or "requestsperday" in low
    delays = [float(m) for m in _RETRY_DELAY_RE.findall(joined)]
    return GeminiQuotaError(GEMINI_QUOTA_MESSAGE, daily=daily, retry_after=max(delays) if delays else None)


GEMINI_UNAVAILABLE_MESSAGE = (
    "Google Gemini зараз перевантажений і не відповідає. "
    "Транскрипцію збережено, спробуйте ще раз через кілька хвилин."
)

GEMINI_QUOTA_MESSAGE = (
    "Ліміт безкоштовного ключа Gemini на зараз вичерпано. Транскрипцію збережено. "
    "Спробуй ще раз за годину, а якщо закінчився денний ліміт, то після 10:00 за Києвом "
    "(Google оновлює його опівночі за тихоокеанським часом)."
)

GEMINI_BAD_KEY_MESSAGE = (
    "Google не приймає ключ Gemini. Відкрий «Налаштування → Розпізнавання й ШІ» "
    "і встав ключ з AI Studio ще раз, повністю. Транскрипцію збережено."
)

_QUOTA_MARKERS = ("429", "resource_exhausted", "quota", "rate limit", "exceeded")
_BAD_KEY_MARKERS = ("api_key_invalid", "api key not valid", "api key expired", "permission_denied", "unauthenticated")


def _gemini_error_kind(err_str: str) -> str:
    low = err_str.lower()
    if any(m in low for m in _BAD_KEY_MARKERS):
        return "bad_key"
    if any(m in low for m in _QUOTA_MARKERS):
        return "quota"
    return "other"


# Pauses (seconds) before the 2nd and 3rd full pass over all candidate models
_RETRY_PASS_DELAYS: tuple[float, ...] = (5.0, 20.0)

_TRANSIENT_MARKERS = (
    "500", "502", "503", "504", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED",
    "high demand", "overloaded", "DEADLINE_EXCEEDED", "INTERNAL", "timeout", "timed out",
)


def _is_transient_error(err_str: str) -> bool:
    low = err_str.lower()
    return any(m.lower() in low for m in _TRANSIENT_MARKERS)


# JSON schemas for Gemini structured output: the model must return exactly this shape,
# so a stray quote or a cut-off sentence in "reason" no longer breaks parsing.
_CLIPS_SCHEMA: dict = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "start": {"type": "number"},
            "end": {"type": "number"},
            "title": {"type": "string"},
            "category": {"type": "string", "enum": ["content", "intro", "outro", "ad"]},
            "hook": {"type": "integer"},
            "emotion": {"type": "integer"},
            "standalone": {"type": "integer"},
            "value": {"type": "integer"},
            "reason": {"type": "string"},
            "hook_text": {"type": "string"},
            "caption": {"type": "string"},
            "hashtags": {"type": "array", "items": {"type": "string"}},
        },
        "required": [
            "start", "end", "title", "category", "hook", "emotion", "standalone", "value",
            "reason", "hook_text", "caption", "hashtags",
        ],
    },
}

_RERANK_SCHEMA: dict = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"n": {"type": "integer"}, "score": {"type": "integer"}},
        "required": ["n", "score"],
    },
}


def _json_config(schema: dict, temperature: float, thinking_level: Optional[str] = None,
                 timeout_ms: Optional[int] = None):
    """generate_content config: JSON that follows the schema, no automatic function calling,
    the thinking level when one is given, and a longer timeout for this request when given."""
    from google.genai import types

    extra = {}
    if thinking_level:
        extra["thinking_config"] = types.ThinkingConfig(thinking_level=thinking_level)
    if timeout_ms:
        extra["http_options"] = types.HttpOptions(timeout=int(timeout_ms))
    return types.GenerateContentConfig(
        response_mime_type="application/json",
        response_json_schema=schema,
        temperature=temperature,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        **extra,
    )


# ─── Request budget: free-tier limits ───────────────────────────────────────
# Free keys allow the Lite models ~15 requests per minute and ~500 per day. One window makes at
# most MAX_ATTEMPTS_PER_WINDOW requests per round, each model gets at most
# gemini.max_rpm_per_model requests in any 60 s, and a model that answered «daily limit» is
# skipped until Google resets it (midnight Pacific time), remembered across restarts.
MAX_ATTEMPTS_PER_WINDOW = 4
QUOTA_STATE_PATH = Path(__file__).resolve().parent / "gemini_quota_state.json"
_quota_state_lock = threading.Lock()


def _attempt_plan(models: list[str]) -> list[str]:
    return [models[i % len(models)] for i in range(MAX_ATTEMPTS_PER_WINDOW)]


def _is_daily_quota(err_str: str) -> bool:
    low = err_str.lower()
    return "perday" in low or "per_day" in low or "per day" in low or "requestsperday" in low


def _exhausted_today(now: Optional[float] = None) -> set[str]:
    """Models whose daily limit is used up and not yet reset."""
    now = time.time() if now is None else now
    with _quota_state_lock:
        try:
            data = json.loads(QUOTA_STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return set()
    if not isinstance(data, dict):
        return set()
    return {m for m, until in data.items() if isinstance(until, (int, float)) and until > now}


def mark_exhausted_today(model_name: str, now: Optional[float] = None) -> None:
    """Remembers that the model's daily limit is used up, until Google resets it."""
    from backend.queue_runner import next_daily_reset  # late import: the queue imports this module

    now = time.time() if now is None else now
    until = next_daily_reset(now) + 5 * 60
    with _quota_state_lock:
        try:
            data = json.loads(QUOTA_STATE_PATH.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                data = {}
        except (OSError, ValueError):
            data = {}
        data = {m: u for m, u in data.items() if isinstance(u, (int, float)) and u > now}
        data[model_name] = until
        try:
            tmp = QUOTA_STATE_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps(data), encoding="utf-8")
            tmp.replace(QUOTA_STATE_PATH)
        except OSError as e:
            log.warning(f"Could not save the Gemini daily-limit note: {e}")
    log.warning(f"{model_name}: денний ліміт безкоштовного ключа вичерпано, не чіпаємо її до оновлення ліміту")


def available_models() -> list[str]:
    """The candidate models in order, without those whose daily limit is used up."""
    gone = _exhausted_today()
    return [m for m in CANDIDATE_MODELS if m not in gone]


def _fallback_models() -> list[str]:
    """Stronger models asked once when every candidate is overloaded (not those out of today's limit)."""
    gone = _exhausted_today()
    extra = getattr(app_config.gemini, "fallback_models", None) or []
    return [m for m in extra if m not in gone and m not in CANDIDATE_MODELS]


# After this many «overloaded» answers in one analysis with no answer at all, Google is down:
# the other windows stop asking instead of burning 4 requests each (and a second round)
OVERLOAD_TRIP = 6


class _OverloadWatch:
    """Shared by the windows of one analysis: counts «overloaded» answers until the first real answer."""

    def __init__(self):
        self._lock = threading.Lock()
        self._fails = 0
        self._answers = 0
        self._fallback_failed = False
        self._fallback_busy = False

    def failed(self, fallback: bool = False) -> None:
        with self._lock:
            self._fails += 1
            if fallback:
                self._fallback_failed = True

    def answered(self) -> None:
        with self._lock:
            self._answers += 1

    def tripped(self) -> bool:
        with self._lock:
            return self._answers == 0 and self._fails >= OVERLOAD_TRIP

    def down(self) -> bool:
        """Nothing answered at all, only «overloaded»."""
        with self._lock:
            return self._answers == 0 and self._fails > 0

    def claim_fallback(self) -> bool:
        """One window at a time tries the fallback model; none after it was overloaded too."""
        with self._lock:
            if self._fallback_failed or self._fallback_busy:
                return False
            self._fallback_busy = True
            return True

    def release_fallback(self) -> None:
        with self._lock:
            self._fallback_busy = False


class _RateLimiter:
    """At most `limit` requests per model in any 60 s, shared by all threads."""

    def __init__(self, clock=None, sleep=None):
        self._lock = threading.Lock()
        self._sent: dict[str, list[float]] = {}
        self._clock = clock or time.monotonic
        self._sleep = sleep or (lambda s: time.sleep(s))

    def acquire(self, model_name: str, limit: int) -> None:
        if limit <= 0:
            return
        while True:
            with self._lock:
                now = self._clock()
                recent = [t for t in self._sent.get(model_name, []) if now - t < 60.0]
                if len(recent) < limit:
                    recent.append(now)
                    self._sent[model_name] = recent
                    return
                self._sent[model_name] = recent
                wait = 60.0 - (now - recent[0]) + 0.05
            log.info(f"{model_name}: {limit} запитів за хвилину вже надіслано, чекаємо {wait:.0f} с")
            self._sleep(wait)


_rate_limiter = _RateLimiter()


def _max_rpm() -> int:
    try:
        return int(app_config.gemini.max_rpm_per_model or 0)
    except (TypeError, ValueError):
        return 0


# Clip selection needs little reasoning: the default thinking of the bigger models made one window
# take 75+ s (and run into 504). gemini.thinking_level ("" = the model's default) is sent to every
# model; a model that rejects it is remembered and asked without it from then on.
_NO_THINKING_LEVEL: set[str] = set()
_NO_THINKING_LOCK = threading.Lock()


def _thinking_level_for(model_name: str, strong: bool = False) -> Optional[str]:
    key = "strong_thinking_level" if strong else "thinking_level"
    level = str(getattr(app_config.gemini, key, "") or "").strip().upper()
    if not level:
        return None
    with _NO_THINKING_LOCK:
        return None if model_name in _NO_THINKING_LEVEL else level


def _generate(client, model_name: str, prompt: str, schema: dict, temperature: float, strong: bool = False):
    """generate_content with schema JSON and the configured thinking level (dropped for a model
    that does not accept it). strong: one of the stronger models of the experimental search, which
    thinks a little more and gets a longer timeout."""
    level = _thinking_level_for(model_name, strong)
    timeout_ms = int(getattr(app_config.gemini, "strong_timeout_sec", 480) or 480) * 1000 if strong else None
    _rate_limiter.acquire(model_name, _max_rpm())
    try:
        return client.models.generate_content(
            model=model_name, contents=prompt, config=_json_config(schema, temperature, level, timeout_ms))
    except Exception as e:
        low = str(e).lower()
        if level and "thinking" in low and ("400" in low or "invalid" in low):
            with _NO_THINKING_LOCK:
                _NO_THINKING_LEVEL.add(model_name)
            log.warning(f"{model_name} does not accept thinking_level={level}; asking without it")
            return client.models.generate_content(
                model=model_name, contents=prompt, config=_json_config(schema, temperature, None, timeout_ms))
        raise


# ─── Stronger models for the experimental search ────────────────────────────
# The experimental search sends 1–2 big requests per video, so it can use a stronger model whose free
# daily limit is small. Each strong model is asked before the candidate models; if none answers,
# the usual plan (candidate models + fallback) runs, so a busy strong model never fails the video.
EXPERIMENTAL_MODEL_AUTO = "auto"
EXPERIMENTAL_MODEL_FAST = "fast"
# A model chosen by name gets this many tries (with a pause) before the search moves on
_CHOSEN_MODEL_WAITS: tuple[float, ...] = (20.0, 40.0)


def _is_paid_only(err_str: str) -> bool:
    """The free tier of this key has no quota for the model at all («limit: 0»)."""
    return re.search(r"limit:\s*0(?![\d.])", err_str) is not None


def strong_plan() -> list[tuple[str, int]]:
    """[(model, tries)] for the experimental search, without models out of today's limit."""
    choice = str(getattr(app_config.gemini, "experimental_model", EXPERIMENTAL_MODEL_AUTO) or EXPERIMENTAL_MODEL_AUTO)
    if choice == EXPERIMENTAL_MODEL_FAST:
        return []
    gone = _exhausted_today()
    if choice == EXPERIMENTAL_MODEL_AUTO:
        models = list(getattr(app_config.gemini, "strong_models", None) or [])
        return [(m, 1) for m in models if m not in gone and m not in CANDIDATE_MODELS]
    # the chosen model gets a few tries, then the other strong models once each, then the fast ones
    others = [m for m in (getattr(app_config.gemini, "strong_models", None) or [])
              if m != choice and m not in gone and m not in CANDIDATE_MODELS]
    first = [] if choice in gone else [(choice, 1 + len(_CHOSEN_MODEL_WAITS))]
    return first + [(m, 1) for m in others]


def _ask_strong(client, plan: list[tuple[str, int]], prompt: str, schema: dict, temperature: float, parse):
    """Asks the strong models in order; returns (parsed answer, model) or (None, None).
    A daily limit (or a model this key cannot use for free) is remembered until Google resets it."""
    for model_name, tries in plan:
        for attempt in range(tries):
            if model_name in _exhausted_today():
                break
            if attempt:
                wait = _CHOSEN_MODEL_WAITS[min(attempt - 1, len(_CHOSEN_MODEL_WAITS) - 1)]
                log.warning(f"{model_name} перевантажена, ще раз через {wait:.0f} с")
                time.sleep(wait)
            started = time.monotonic()
            try:
                log.info(f"Trying strong Gemini model: {model_name} (try {attempt + 1}/{tries})")
                response = _generate(client, model_name, prompt, schema, temperature, strong=True)
                raw_text = (response.text or "").strip() if response else ""
                if not raw_text:
                    log.warning(f"{model_name}: порожня відповідь")
                    break
                result = parse(raw_text)
                log.info(f"Gemini {model_name} answered in {time.monotonic() - started:.1f} s")
                return result, model_name
            except Exception as e:
                err = str(e)
                kind = _gemini_error_kind(err)
                if kind == "quota" and (_is_daily_quota(err) or _is_paid_only(err)):
                    mark_exhausted_today(model_name)
                    break
                if kind == "bad_key":
                    return None, None  # the candidate models will report the wrong key
                low = err.lower()
                if "timed out" in low or "timeout" in low or "deadline" in low:
                    # a long video took too long: the next strong model would wait as long, go to the fast ones
                    log.warning(f"{model_name} не встигла за {time.monotonic() - started:.0f} с, беремо швидкі моделі")
                    return None, None
                if _is_transient_error(err) and kind != "quota":
                    log.info(f"⚡ Сильна модель {model_name} зараз недоступна ({err[:120]})")
                    continue
                log.warning(f"Strong model {model_name} failed: {err[:300]}")
                break
    if plan:
        log.warning("Сильні моделі Gemini не відповіли, беремо звичайні швидкі")
    return None, None


def _call_gemini_single_window(
    client,
    segments: list[dict],
    count: int,
    min_len: int,
    max_len: int,
    filter_mode: str,
    detected_lang: str,
    target_len: Optional[float] = None,
    gate: Optional["_SharedBackoff"] = None,
    experimental: bool = False,
    max_chars: int = _MAX_TRANSCRIPT_CHARS,
    overload: Optional["_OverloadWatch"] = None,
    strong: Optional[list[tuple[str, int]]] = None,
) -> list[dict]:
    """Sends a single chunk of transcript segments to Gemini and returns parsed clips.
    gate: shared with the other parallel windows (a 503/429 here makes all of them hold off).
    overload: shared by the windows of one analysis; once Google is clearly down, stop asking."""
    transcript_lines = [f"[{s['start']}s - {s['end']}s]: {s['text']}" for s in segments]
    transcript_text = "\n".join(transcript_lines)

    # Safe guard: if a single chunk still exceeds limit, truncate with notice
    if len(transcript_text) > max_chars:
        log.warning(
            f"Transcript chunk length ({len(transcript_text)} chars) exceeds {max_chars}. "
            "Applying safe boundary truncation."
        )
        transcript_text = transcript_text[:max_chars] + "\n... [транскрипцію скорочено для безпеки контексту]"

    if target_len:
        length_hint = (
            f"  Орієнтовна середня тривалість близько {int(round(target_len))} с: бери думку повністю, "
            "з підводкою і розв'язкою, але не розтягуй."
        )
    else:
        length_hint = "  Бери думку повністю, з підводкою і розв'язкою; не обривай на півслові."

    canonical_mode = resolve_filter_mode(filter_mode)
    filter_hint = FILTER_INSTRUCTIONS.get(canonical_mode, "Цікаві завершені думки")

    lang = language_name(detected_lang)
    if (detected_lang or "uk").lower() == "uk":
        lang_rule = "Усі тексти ('title', 'reason', 'hook_text', 'caption', 'hashtags') пиши українською."
    else:
        lang_rule = (
            f"Мова відео — {lang}. 'title', 'hook_text', 'caption' і 'hashtags' пиши МОВОЮ ВІДЕО ({lang}): "
            "вони підуть у публікацію для глядачів цього відео. 'reason' пиши українською: це пояснення для автора. "
            "Приклад JSON нижче українською лише для формату."
        )

    reaction_hint = ""
    if "[реакція:" in transcript_text:  # stream recordings: reactions.mark_segments
        source = "у цю мить звук різко голоснішав (сміх, оплески, крик) або чат вибухнув повідомленнями"
        if "глядачі YouTube" in transcript_text:
            source += ", або це місце глядачі YouTube переглядали найчастіше"
        if experimental:
            reaction_hint = (
                f"\nПозначки «[реакція: ...]» — не мовлення, а підказки: {source}. Такі місця часто найвірусніші: "
                "перевір їх першими й бери, якщо там справді є момент, з підводкою за кілька секунд до реакції. "
                "Не копіюй позначки в 'hook_text' і не обирай кліп лише через позначку.\n"
            )
        else:
            reaction_hint = (
                "\nЦе запис стріму. Позначки «[реакція: ...]» — не мовлення, а підказки: у цю мить стрімер різко "
                "голоснішав (крик, сміх) або чат вибухнув повідомленнями. Такі місця часто найвірусніші: перевір їх "
                "першими й бери, якщо там справді є момент, з підводкою за кілька секунд до реакції. "
                "Не копіюй позначки в 'hook_text' і не обирай кліп лише через позначку.\n"
            )
    if experimental:
        scope = "УСЬОГО відео" if max_chars >= EXPERIMENTAL_MAX_CHARS else "фрагмента відео"
        count_rule = (
            f"- Кількість фрагментів: ДО {count}. Бери лише справді сильні моменти: краще менше, ніж слабкі.\n"
            "- НЕ розподіляй кліпи рівномірно по відео. Якщо найцікавіше зібрано в одному місці, бери кілька кліпів звідти; "
            "нудні частини пропускай повністю.\n"
            "- Упорядкуй масив від найсильнішого моменту до найслабшого."
        )
        hook_rule = (
            "\nХУК: 'start' — секунда, де звучить перша сильна фраза, що чіпляє глядача за 1-2 секунди "
            "(питання, суперечливе твердження, несподіваний факт, початок жарту). НЕ починай з розгону: "
            "«ну», «отже», «так от», «як я казав», повторів, звертань до чату чи пауз. Якщо для розуміння "
            "потрібна підводка, вона має бути короткою. 'hook_text' — ДОСЛІВНО перші 5-8 слів кліпу з транскрипції.\n"
        )
    else:
        scope = "фрагмента відео"
        count_rule = f"- Кількість фрагментів: рівно {count}"
        hook_rule = ""

    prompt = f"""\
Ти — топ-продюсер вертикального контенту (Shorts/Reels/TikTok).
Ось транскрипція {scope} (мова: {lang}) з таймкодами:
{transcript_text}
{reaction_hint}
Параметри завдання:
{count_rule}
- Тривалість кожного кліпу: СТРОГО від {min_len} до {max_len} секунд (end - start).
{length_hint}
- Стиль вибору: {filter_hint}

Обирай НАЙВІРУСНІШІ моменти: сильний хук у перші 1-3 секунди, емоція, несподіванка,
суперечлива думка, смішний або цінний інсайт. Кожен кліп має бути САМОДОСТАТНІМ:
зрозумілим без решти відео, починатися з початку речення й закінчуватися завершеною фразою
(не обривай речення). Кліпи НЕ повинні перетинатися між собою.
НЕ обирай вступ і привітання, прощання, заклики підписатися та рекламні інтеграції;
якщо такий фрагмент усе ж потрапив у список, познач його відповідною категорією.
{hook_rule}
Вимоги до кожного знайденого кліпу:
1. 'start' і 'end' — точні секунди (числа) з таймкодів транскрипції.
2. 'title' — короткий чіпкий заголовок КАПСЛОКОМ мовою відео, що розкриває головну тему моменту (до 5 слів).
3. 'category' — одне з: "content" (корисний момент), "intro" (вступ/привітання), "outro" (прощання, заклик підписатися), "ad" (реклама, інтеграція, промокод).
4. Оцінки від 1 до 10 (цілі числа), будь СУВОРИМ і використовуй усю шкалу — середній момент це 5, а не 8:
   'hook' — наскільки перші 3 секунди змушують дивитися далі;
   'emotion' — емоція, гумор, конфлікт, несподіванка;
   'standalone' — наскільки кліп зрозумілий без контексту і має завершену думку;
   'value' — користь, інсайт або цікавий факт для глядача.
5. 'reason' — 1-2 речення: про що саме цей момент і чому він захопить глядача.
6. 'hook_text' — точна перша фраза з транскрипції, з якої починається фрагмент.
7. 'caption' — захоплюючий текст-опис для публікації в соцмережах (1-2 речення).
8. 'hashtags' — список із 4 релевантних хештегів, наприклад: ["#shorts", "#інсайти", "#тренди", "#мотивація"].

Мова: {lang_rule}

Поверни результат ВИКЛЮЧНО у валідному JSON масиві об'єктів:
[{{"start": 10.0, "end": 42.5, "title": "ГОЛОВНА ПОМИЛКА", "category": "content", "hook": 8, "emotion": 6, "standalone": 9, "value": 7, "reason": "Розповідає про фатальну помилку підприємців і чим вона закінчилась.", "hook_text": "Найбільша помилка, якої припускаються майже всі...", "caption": "Головна помилка, якої припускаються майже всі! Дивіться до кінця.", "hashtags": ["#shorts", "#помилки", "#інсайти", "#тренди"]}}]
"""
    if strong:
        clips, used = _ask_strong(client, strong, prompt, _CLIPS_SCHEMA, 0.3, _parse_clips_json)
        if clips is not None:
            return [{**c, "_model": used} for c in clips]

    last_err = ""
    error_kinds: list[str] = []
    quota_errors: list[str] = []

    models = available_models()
    if not models:
        log.warning("Усі моделі Gemini вичерпали денний ліміт; чекаємо, поки Google його оновить")
        raise quota_error_from(["per day limit (remembered)"])

    # At most MAX_ATTEMPTS_PER_WINDOW requests: the models in turn, with a growing pause each time
    # the list starts over (instead of 3 full passes over every model)
    overload = overload or _OverloadWatch()
    gave_up = False
    plan = _attempt_plan(models)
    # the fallback is a stronger model with a small free daily limit: only the experimental search
    # (1-3 requests per video) uses it, the classic one (many windows) would use it up
    for attempt, model_name in enumerate(plan + (_fallback_models() if experimental else [])):
        if error_kinds and all(k == "bad_key" for k in error_kinds):
            break  # a wrong key will not start working after a pause
        fallback = attempt >= len(plan)
        if fallback:
            # the candidates all said «overloaded»: one try of a stronger model from another family,
            # but only once per analysis if it is overloaded too
            if not error_kinds or any(k != "overload" for k in error_kinds) or not overload.claim_fallback():
                break
            log.warning(f"Основні моделі Gemini перевантажені, пробуємо запасну {model_name}")
        elif overload.tripped():
            if not gave_up:
                log.warning("Сервери Google перевантажені для всіх частин відео: не витрачаємо більше запитів")
                gave_up = True
            if error_kinds:
                continue  # straight to the fallback model
            error_kinds.append("overload")
            last_err = "Google overloaded (other windows)"
            continue
        if attempt and not fallback and attempt % len(models) == 0:
            delay = _RETRY_PASS_DELAYS[min(attempt // len(models) - 1, len(_RETRY_PASS_DELAYS) - 1)]
            log.warning(
                f"Жодна модель Gemini не відповіла (спроба {attempt}/{MAX_ATTEMPTS_PER_WINDOW}). "
                f"Повтор через {delay:.0f} с..."
            )
            time.sleep(delay)
        if model_name in _exhausted_today():
            continue  # another window found its daily limit meanwhile
        try:
            if gate is not None and not fallback:
                gate.wait()
            log.info(f"Trying Gemini model: {model_name} (attempt {attempt + 1}/{MAX_ATTEMPTS_PER_WINDOW})")
            started = time.monotonic()
            try:
                response = _generate(client, model_name, prompt, _CLIPS_SCHEMA, temperature=0.3)
            finally:
                if fallback:
                    overload.release_fallback()
            log.info(f"Gemini {model_name} answered in {time.monotonic() - started:.1f} s")
            raw_text = (response.text or "").strip() if response else ""
            if not raw_text:
                last_err = f"{model_name}: порожня відповідь"
                log.warning(f"Model {model_name} returned an empty response, trying next model...")
                continue
        except Exception as e:
            last_err = f"{model_name}: {e}"
            kind = _gemini_error_kind(str(e))
            if kind == "other" and _is_transient_error(str(e)):
                kind = "overload"
            error_kinds.append(kind)
            if kind == "overload":
                overload.failed(fallback)
            if error_kinds[-1] == "quota":
                quota_errors.append(str(e))
                if _is_daily_quota(str(e)):
                    mark_exhausted_today(model_name)
            if _is_transient_error(str(e)):
                if gate is not None:
                    gate.hit()
                log.info(f"⚡ Модель {model_name} тимчасово недоступна ({e}), перемикаємось на наступну...")
            else:
                log.warning(f"Model {model_name} failed: {e}, trying next model...")
            continue

        try:
            clips = _parse_clips_json(raw_text)
            log.info(f"Response successfully received and parsed from {model_name}")
            overload.answered()
            return [{**c, "_model": model_name} for c in clips]
        except Exception as e:
            last_err = f"{model_name}: {e}"
            log.warning(f"Помилка парсингу відповіді {model_name}: {e}. Пробуємо наступну модель...")

    log.error(f"Усі моделі Gemini недоступні. Остання помилка: {last_err}")
    if error_kinds and all(k == "bad_key" for k in error_kinds):
        raise GeminiUnavailableError(GEMINI_BAD_KEY_MESSAGE)
    if error_kinds and all(k == "quota" for k in error_kinds):
        raise quota_error_from(quota_errors)
    raise GeminiUnavailableError(GEMINI_UNAVAILABLE_MESSAGE)


# Long videos: transcript windows go to Gemini in parallel (gemini.parallel_windows at a time).
# A per-minute limit hit on one window pauses that window and tries it again instead of losing it.
_RATE_RETRY_WAITS: tuple[float, ...] = (30.0, 60.0)
_RATE_RETRY_MAX_WAIT = 90.0
# Windows Google did not answer (overloaded) get one more round after the others, after this pause.
_SECOND_ROUND_WAIT = 90.0
# When one parallel window gets 503/429/504, all of them hold off this long (plus jitter).
_GATE_PAUSE = (2.0, 4.0)

GEMINI_PARTIAL_OVERLOAD_MESSAGE = (
    "Сервери Google перевантажені: проаналізовано {done} з {total} частин відео. "
    "Готові частини збережено, решту доробимо пізніше."
)


class GeminiIncompleteError(GeminiUnavailableError):
    """Some transcript windows got no answer even after the second round.
    done: {window key: clips} of the windows that did answer (to be kept and reused);
    cause: the error of a missing window. daily / retry_after / overloaded drive the queue pause."""

    def __init__(self, message: str, done: dict, total: int, cause: Exception):
        super().__init__(message)
        self.done = done
        self.total = total
        self.cause = cause
        self.daily = bool(getattr(cause, "daily", False))
        self.retry_after = getattr(cause, "retry_after", None)
        self.overloaded = not isinstance(cause, GeminiQuotaError)


class _SharedBackoff:
    """A gate shared by the parallel windows of one analysis: after a 503/429/504 anywhere,
    every window waits a little before its next request instead of hammering all models at once."""

    def __init__(self):
        self._lock = threading.Lock()
        self._until = 0.0

    def hit(self, seconds: Optional[float] = None) -> None:
        delay = seconds if seconds is not None else random.uniform(*_GATE_PAUSE)
        with self._lock:
            self._until = max(self._until, time.monotonic() + delay)

    def wait(self) -> None:
        with self._lock:
            delay = self._until - time.monotonic()
        if delay > 0:
            time.sleep(delay)


def _parallel_windows() -> int:
    try:
        return max(1, int(app_config.gemini.parallel_windows or 1))
    except (TypeError, ValueError):
        return 1


def _window_with_rate_retry(index: int, total: int, **kwargs) -> list[dict]:
    """One window; on a per-minute limit waits (Google's retry delay if given) and tries again.
    The per-day limit and other errors go up unchanged."""
    for attempt in range(len(_RATE_RETRY_WAITS) + 1):
        log.info(f"Analyzing transcript window {index + 1}/{total}...")
        try:
            return _call_gemini_single_window(**kwargs)
        except GeminiQuotaError as e:
            if e.daily or attempt >= len(_RATE_RETRY_WAITS):
                raise
            wait = min(_RATE_RETRY_MAX_WAIT, max(_RATE_RETRY_WAITS[attempt], (e.retry_after or 0) + 2))
            log.warning(f"Window {index + 1}: Gemini per-minute limit, trying again in {wait:.0f} s")
            time.sleep(wait)
    raise AssertionError("unreachable")


def window_key(segments: list[dict], count: int, min_len: int, max_len: int, filter_mode: str,
               detected_lang: str, target_len: Optional[float]) -> str:
    """Identifies one window's request, so a finished window can be reused after a pause or restart."""
    sig = {
        "n": len(segments),
        "first": [segments[0]["start"], segments[0]["end"]] if segments else None,
        "last": [segments[-1]["start"], segments[-1]["end"]] if segments else None,
        "chars": sum(len(s.get("text", "")) for s in segments),
        "count": count, "min": min_len, "max": max_len,
        "filter": resolve_filter_mode(filter_mode), "lang": detected_lang,
        "target": round(float(target_len), 1) if target_len else None,
    }
    return hashlib.sha1(json.dumps(sig, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def _retry_later(err: Exception) -> bool:
    """Worth a second round: Google overloaded or did not answer (not a wrong key, not a limit)."""
    if isinstance(err, GeminiQuotaError):
        return False
    return str(err) != GEMINI_BAD_KEY_MESSAGE


def analyze_and_find_clips(
    client=None,
    segments: list[dict] = None,
    count: int = 3,
    min_len: int = 15,
    max_len: int = 60,
    filter_mode: str = FilterMode.VIRAL_HOOKS,
    detected_lang: str = "uk",
    target_len: Optional[float] = None,
    done_windows: Optional[dict] = None,
) -> list[dict]:
    """
    High-level AI clip selector with context protection and smart chunking.

    The transcript goes to Gemini as one prompt, or for long videos / many clips as chronological
    windows analysed in parallel and merged. Windows Google did not answer get a second round after
    the others; if some are still missing, GeminiIncompleteError carries the finished ones
    (done_windows on the next call reuses them, so only the missing windows are asked again).
    A wrong key raises at once. Results are never partial without saying so.
    """
    if client is None:
        client = get_gemini_client()

    segments = segments or []
    if not segments:
        return []

    # Estimate transcript volume in characters
    total_chars = sum(len(s.get("text", "")) + 25 for s in segments)

    # Maximum clips to request in a single prompt to avoid model output token truncation
    max_clips_per_prompt = 15
    chunks_by_chars = math.ceil(total_chars / _MAX_TRANSCRIPT_CHARS) if total_chars > _MAX_TRANSCRIPT_CHARS else 1
    chunks_by_count = math.ceil(count / max_clips_per_prompt) if count > max_clips_per_prompt else 1
    num_chunks = max(chunks_by_chars, chunks_by_count)

    if num_chunks <= 1:
        windows = [segments]
        per_window = count
    else:
        chunk_size = math.ceil(len(segments) / num_chunks)
        per_window = max(1, math.ceil(count / num_chunks))
        windows = [w for w in (segments[i * chunk_size: (i + 1) * chunk_size] for i in range(num_chunks)) if w]
        log.info(
            f"Partitioning analysis ({total_chars} chars across {len(segments)} segments, target {count} clips) "
            f"into {len(windows)} chronological windows (~{per_window} clips per window)."
        )

    keys = [window_key(w, per_window, min_len, max_len, filter_mode, detected_lang, target_len) for w in windows]
    done: dict = {k: v for k, v in (done_windows or {}).items() if k in keys}
    if done:
        log.info(f"Reusing {len(done)} of {len(windows)} windows analysed before; asking Gemini only for the rest")
    workers = max(1, min(_parallel_windows(), len(windows)))
    gate = _SharedBackoff() if workers > 1 else None
    overload = _OverloadWatch()

    def run(i: int) -> list[dict]:
        return _window_with_rate_retry(
            i, len(windows),
            client=client,
            segments=windows[i],
            count=per_window,
            min_len=min_len,
            max_len=max_len,
            filter_mode=filter_mode,
            detected_lang=detected_lang,
            target_len=target_len,
            gate=gate,
            overload=overload,
        )

    pending = [i for i, k in enumerate(keys) if k not in done]
    errors: dict[int, Exception] = {}
    for round_no in (1, 2):
        if not pending:
            break
        if round_no == 2:
            pending = [i for i in pending if _retry_later(errors[i])]
            if not pending:
                break
            if overload.down() and not done:
                log.warning("Google did not answer a single window: no second round, try again later")
                break
            if _SECOND_ROUND_WAIT > 0:
                log.warning(
                    f"Gemini did not answer {len(pending)} window(s); trying them again in {_SECOND_ROUND_WAIT:.0f} s"
                )
                time.sleep(_SECOND_ROUND_WAIT)
        if workers > 1 and len(pending) > 1:
            log.info(f"Analyzing {len(pending)} windows, up to {workers} at a time")
            with ThreadPoolExecutor(max_workers=min(workers, len(pending)), thread_name_prefix="GeminiWindow") as pool:
                futures = {i: pool.submit(run, i) for i in pending}
            outcomes = {}
            for i, fut in futures.items():
                try:
                    outcomes[i] = fut.result()
                except Exception as e:
                    outcomes[i] = e
        else:
            outcomes = {}
            for i in pending:
                try:
                    outcomes[i] = run(i)
                except Exception as e:
                    outcomes[i] = e
        failed = []
        for i in pending:
            out = outcomes[i]
            if isinstance(out, Exception):
                errors[i] = out
                failed.append(i)
                log.warning(f"Window {i + 1} analysis failed: {out}")
            else:
                done[keys[i]] = out
                errors.pop(i, None)
        pending = failed
        if any(str(errors[i]) == GEMINI_BAD_KEY_MESSAGE for i in pending):
            break  # a wrong key will not start working in a second round

    missing = [i for i in range(len(windows)) if keys[i] not in done]
    if missing:
        causes = [errors.get(i) for i in missing if errors.get(i) is not None]
        bad = next((e for e in causes if str(e) == GEMINI_BAD_KEY_MESSAGE), None)
        if bad is not None:
            raise bad
        cause = next((e for e in causes if isinstance(e, GeminiQuotaError)), None) or (
            causes[0] if causes else GeminiUnavailableError(GEMINI_UNAVAILABLE_MESSAGE))
        n_done = len(windows) - len(missing)
        finished = {k: v for k, v in done.items() if k in keys}
        if isinstance(cause, GeminiQuotaError):
            message = str(cause) if n_done == 0 else (
                f"Проаналізовано {n_done} з {len(windows)} частин відео, решту не дав ліміт ключа. {cause}")
        elif n_done == 0:
            message = GEMINI_UNAVAILABLE_MESSAGE
        else:
            message = GEMINI_PARTIAL_OVERLOAD_MESSAGE.format(done=n_done, total=len(windows))
        log.error(f"Gemini: {len(missing)} of {len(windows)} windows still missing ({cause})")
        raise GeminiIncompleteError(message, finished, len(windows), cause)

    if len(windows) == 1:
        return done[keys[0]]

    # Merge in window order, so the result is the same however the windows were scheduled
    all_clips: list[dict] = []
    for k in keys:
        all_clips.extend(done[k])
    # Sort candidates by score descending (if present), else chronological
    all_clips.sort(key=lambda c: (float(c.get("score", 0)), -c.get("start", 0)), reverse=True)
    selected = all_clips[:count]
    # Re-order chronologically by start time for consistent viewing
    selected.sort(key=lambda c: c["start"])
    return selected


def _overlapping_windows(segments: list[dict], max_chars: int, overlap_sec: float) -> list[list[dict]]:
    """Splits segments into windows of at most ~max_chars; each next window repeats the last overlap_sec."""
    sizes = [len(s.get("text", "")) + 25 for s in segments]
    if sum(sizes) <= max_chars:
        return [segments]
    windows: list[list[dict]] = []
    i = 0
    while i < len(segments):
        total, j = 0, i
        while j < len(segments) and (total + sizes[j] <= max_chars or j == i):
            total += sizes[j]
            j += 1
        windows.append(segments[i:j])
        if j >= len(segments):
            break
        # next window starts overlap_sec before this one ends, but always moves forward
        back_to = float(segments[j - 1].get("end", 0)) - overlap_sec
        k = j
        while k - 1 > i and float(segments[k - 1].get("start", 0)) >= back_to:
            k -= 1
        i = max(i + 1, k)
    return windows


def find_clips_experimental(
    client=None,
    segments: Optional[list[dict]] = None,
    count: int = 10,
    min_len: int = 15,
    max_len: int = 60,
    filter_mode: str = FilterMode.VIRAL_HOOKS,
    detected_lang: str = "uk",
    target_len: Optional[float] = None,
) -> list[dict]:
    """
    «Експериментальний» search: Gemini reads the whole video at once and picks only the strongest
    moments (up to `count`, fewer is fine), clustered where the video is interesting, each starting
    on its hook. Videos longer than one prompt use windows that overlap by 2 minutes, so a moment on
    a window edge is not cut; duplicates from the overlap are removed by score later (clip_refiner).
    """
    if client is None:
        client = get_gemini_client()
    segments = segments or []
    if not segments:
        return []
    count = max(1, min(int(count), EXPERIMENTAL_MAX_CLIPS))
    windows = _overlapping_windows(segments, EXPERIMENTAL_MAX_CHARS, EXPERIMENTAL_OVERLAP_SEC)
    per_window = count if len(windows) == 1 else min(EXPERIMENTAL_MAX_CLIPS, math.ceil(count * 1.5 / len(windows)))
    log.info(f"Experimental search: {len(windows)} window(s), up to {per_window} clips each (target {count}).")
    strong = strong_plan()

    found: list[dict] = []
    gemini_error: Optional[GeminiUnavailableError] = None
    for i, win in enumerate(windows):
        try:
            found.extend(_call_gemini_single_window(
                client=client, segments=win, count=per_window, min_len=min_len, max_len=max_len,
                filter_mode=filter_mode, detected_lang=detected_lang, target_len=target_len,
                experimental=True, max_chars=EXPERIMENTAL_MAX_CHARS, strong=strong_plan() if i else strong,
            ))
        except GeminiUnavailableError as e:
            gemini_error = e
            log.warning(f"Experimental window {i + 1} failed: Gemini unavailable")
        except Exception as e:
            log.warning(f"Experimental window {i + 1} failed: {e}")
    if not found:
        if gemini_error is not None:
            raise gemini_error
        raise RuntimeError("Не вдалося знайти жодного кліпу.")
    for c in found:
        r = rubric_score(c)
        if r is not None:
            c["score"] = r
    return found


# ─── Second pass: comparative re-ranking ────────────────────────────────────

def rerank_clips(
    clips: list[dict],
    filter_mode: str = FilterMode.VIRAL_HOOKS,
    detected_lang: str = "uk",
    client=None,
    strong_model: Optional[str] = None,
) -> list[dict]:
    """
    Second Gemini pass: shows the model all final clips side by side and asks for a comparative
    0–100 score, so the ranking reflects how clips compare to each other instead of each being
    judged alone. The final score blends this with the rubric score. Any failure keeps the
    original scores (the clips themselves are never lost here).
    strong_model: the strong model that found the clips (experimental search) is asked first, once.
    """
    if len(clips) < 2:
        return clips
    try:
        if client is None:
            client = get_gemini_client()
        filter_hint = FILTER_INSTRUCTIONS.get(resolve_filter_mode(filter_mode), "")
        lines = []
        for i, c in enumerate(clips, 1):
            dur = int(float(c["end"]) - float(c["start"]))
            lines.append(
                f"{i}. [{dur} с] {c.get('title', '')} | Початок: «{str(c.get('hook_text', ''))[:160]}» | {str(c.get('reason', ''))[:200]}"
            )
        prompt = (
            "Ти — продюсер Shorts/Reels. Нижче всі кліпи, вирізані з одного відео "
            f"(мова: {language_name(detected_lang)}). Критерій: {filter_hint}\n\n" + "\n".join(lines) + "\n\n"
            "Порівняй кліпи МІЖ СОБОЮ і дай кожному оцінку від 0 до 100, наскільки він набере перегляди. "
            "Використовуй усю шкалу: найкращий близько 95, найслабший близько 30, не став однакові оцінки без причини.\n"
            'Поверни ВИКЛЮЧНО JSON масив: [{"n": 1, "score": 87}, ...] для всіх кліпів.'
        )
        last_err = ""
        first = [strong_model] if strong_model and strong_model not in _exhausted_today() else []
        for model_name in first + [m for m in available_models() if m not in first]:
            try:
                response = _generate(client, model_name, prompt, _RERANK_SCHEMA, temperature=0.2,
                                     strong=model_name in first)
                data = json.loads(_strip_fences((response.text or "").strip()))
                if isinstance(data, dict):
                    data = next((v for v in data.values() if isinstance(v, list)), [])
                ranks = {int(d["n"]): int(d["score"]) for d in data if isinstance(d, dict) and "n" in d and "score" in d}
                if len(ranks) < len(clips) // 2:
                    raise RuntimeError(f"оцінено лише {len(ranks)} з {len(clips)} кліпів")
                out = []
                for i, c in enumerate(clips, 1):
                    if i in ranks:
                        comp = max(0, min(100, ranks[i]))
                        c = {**c, "score": int(round(0.5 * float(c.get("score", 60)) + 0.5 * comp))}
                    out.append(c)
                log.info(f"Re-ranking pass done with {model_name}: {len(ranks)}/{len(clips)} clips scored")
                return out
            except Exception as e:
                last_err = f"{model_name}: {e}"
                if _gemini_error_kind(str(e)) == "quota" and _is_daily_quota(str(e)):
                    mark_exhausted_today(model_name)
                continue
        log.warning(f"Re-ranking pass skipped, keeping rubric scores ({last_err})")
    except Exception as e:
        log.warning(f"Re-ranking pass skipped, keeping rubric scores ({e})")
    return clips


def _strip_fences(text: str) -> str:
    if "```" in text:
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
        text = re.sub(r"```$", "", text, flags=re.MULTILINE).strip()
    return text
