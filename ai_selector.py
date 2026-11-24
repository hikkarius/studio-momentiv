"""
ai_selector.py — Gemini AI clip selection, prompt engineering, context chunking, and boundary validation.
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from typing import Optional

from config_loader import CANDIDATE_MODELS, get_gemini_client
from clip_refiner import rubric_score
from constants import FILTER_INSTRUCTIONS, FilterMode, resolve_filter_mode
from languages import language_name

log = logging.getLogger("shorts_core")

# Safe character ceiling for transcript content inside a single Gemini prompt
# ~100,000 characters corresponds to ~20,000-25,000 spoken words (~2 hours of speech).
_MAX_TRANSCRIPT_CHARS: int = 100_000


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


def _json_config(schema: dict, temperature: float):
    """generate_content config: JSON that follows the schema, no automatic function calling."""
    from google.genai import types

    return types.GenerateContentConfig(
        response_mime_type="application/json",
        response_json_schema=schema,
        temperature=temperature,
        automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
    )


def _call_gemini_single_window(
    client,
    segments: list[dict],
    count: int,
    min_len: int,
    max_len: int,
    filter_mode: str,
    detected_lang: str,
    target_len: Optional[float] = None,
) -> list[dict]:
    """Sends a single chunk of transcript segments to Gemini and returns parsed clips."""
    transcript_lines = [f"[{s['start']}s - {s['end']}s]: {s['text']}" for s in segments]
    transcript_text = "\n".join(transcript_lines)

    # Safe guard: if a single chunk still exceeds limit, truncate with notice
    if len(transcript_text) > _MAX_TRANSCRIPT_CHARS:
        log.warning(
            f"Transcript chunk length ({len(transcript_text)} chars) exceeds {_MAX_TRANSCRIPT_CHARS}. "
            "Applying safe boundary truncation."
        )
        transcript_text = transcript_text[:_MAX_TRANSCRIPT_CHARS] + "\n... [транскрипцію скорочено для безпеки контексту]"

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

    prompt = f"""\
Ти — топ-продюсер вертикального контенту (Shorts/Reels/TikTok).
Ось транскрипція фрагмента відео (мова: {lang}) з таймкодами:
{transcript_text}

Параметри завдання:
- Кількість фрагментів: рівно {count}
- Тривалість кожного кліпу: СТРОГО від {min_len} до {max_len} секунд (end - start).
{length_hint}
- Стиль вибору: {filter_hint}

Обирай НАЙВІРУСНІШІ моменти: сильний хук у перші 1-3 секунди, емоція, несподіванка,
суперечлива думка, смішний або цінний інсайт. Кожен кліп має бути САМОДОСТАТНІМ:
зрозумілим без решти відео, починатися з початку речення й закінчуватися завершеною фразою
(не обривай речення). Кліпи НЕ повинні перетинатися між собою.
НЕ обирай вступ і привітання, прощання, заклики підписатися та рекламні інтеграції;
якщо такий фрагмент усе ж потрапив у список, познач його відповідною категорією.

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
    last_err = ""
    total_passes = len(_RETRY_PASS_DELAYS) + 1
    error_kinds: list[str] = []

    for pass_idx in range(total_passes):
        if error_kinds and all(k == "bad_key" for k in error_kinds):
            break  # a wrong key will not start working after a pause
        if pass_idx > 0:
            delay = _RETRY_PASS_DELAYS[pass_idx - 1]
            log.warning(
                f"Жодна модель Gemini не відповіла (прохід {pass_idx}/{total_passes}). "
                f"Повтор через {delay:.0f} с..."
            )
            time.sleep(delay)

        for model_name in CANDIDATE_MODELS:
            try:
                log.info(f"Trying Gemini model: {model_name} (pass {pass_idx + 1}/{total_passes})")
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=_json_config(_CLIPS_SCHEMA, temperature=0.3),
                )
                raw_text = (response.text or "").strip() if response else ""
                if not raw_text:
                    last_err = f"{model_name}: порожня відповідь"
                    log.warning(f"Model {model_name} returned an empty response, trying next model...")
                    continue
            except Exception as e:
                last_err = f"{model_name}: {e}"
                error_kinds.append(_gemini_error_kind(str(e)))
                if _is_transient_error(str(e)):
                    log.info(f"⚡ Модель {model_name} тимчасово недоступна ({e}), перемикаємось на наступну...")
                else:
                    log.warning(f"Model {model_name} failed: {e}, trying next model...")
                continue

            try:
                clips = _parse_clips_json(raw_text)
                log.info(f"Response successfully received and parsed from {model_name}")
                return clips
            except Exception as e:
                last_err = f"{model_name}: {e}"
                log.warning(f"Помилка парсингу відповіді {model_name}: {e}. Пробуємо наступну модель...")

    log.error(f"Усі моделі Gemini недоступні після {total_passes} проходів. Остання помилка: {last_err}")
    if error_kinds and all(k == "bad_key" for k in error_kinds):
        raise GeminiUnavailableError(GEMINI_BAD_KEY_MESSAGE)
    if error_kinds and all(k == "quota" for k in error_kinds):
        raise GeminiUnavailableError(GEMINI_QUOTA_MESSAGE)
    raise GeminiUnavailableError(GEMINI_UNAVAILABLE_MESSAGE)


def analyze_and_find_clips(
    client=None,
    segments: list[dict] = None,
    count: int = 3,
    min_len: int = 15,
    max_len: int = 60,
    filter_mode: str = FilterMode.VIRAL_HOOKS,
    detected_lang: str = "uk",
    target_len: Optional[float] = None,
) -> list[dict]:
    """
    High-level AI clip selector with context protection and smart chunking.

    For normal-length videos, executes a single-pass Gemini call.
    For very long transcripts (>100k chars / 2+ hours), automatically partitions
    the transcript into chronological windows, queries Gemini for the best highlights
    in each window, and merges the top clips.
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
        # Standard single-pass analysis
        return _call_gemini_single_window(
            client=client,
            segments=segments,
            count=count,
            min_len=min_len,
            max_len=max_len,
            filter_mode=filter_mode,
            detected_lang=detected_lang,
            target_len=target_len,
        )

    # Partitioning into chronological windows for scale & quality
    chunk_size = math.ceil(len(segments) / num_chunks)
    clips_per_chunk = max(1, math.ceil(count / num_chunks))

    log.info(
        f"Partitioning analysis ({total_chars} chars across {len(segments)} segments, target {count} clips) "
        f"into {num_chunks} chronological windows (~{clips_per_chunk} clips per window)."
    )

    all_clips: list[dict] = []
    gemini_error: Optional[GeminiUnavailableError] = None
    for i in range(num_chunks):
        chunk_segs = segments[i * chunk_size: (i + 1) * chunk_size]
        if not chunk_segs:
            continue
        try:
            log.info(f"Analyzing transcript window {i + 1}/{num_chunks}...")
            clips = _call_gemini_single_window(
                client=client,
                segments=chunk_segs,
                count=clips_per_chunk,
                min_len=min_len,
                max_len=max_len,
                filter_mode=filter_mode,
                detected_lang=detected_lang,
                target_len=target_len,
            )
            all_clips.extend(clips)
        except GeminiUnavailableError as e:
            gemini_error = e
            log.warning(f"Window {i + 1} analysis failed: Gemini unavailable")
        except Exception as e:
            log.warning(f"Window {i + 1} analysis failed: {e}")

    if not all_clips:
        if gemini_error is not None:
            raise gemini_error
        raise RuntimeError("Не вдалося знайти жодного кліпу у вікнах аналізу довгого відео.")
    if gemini_error is not None:
        log.warning("Частина вікон не проаналізована через недоступність Gemini; повертаємо кліпи з успішних вікон.")

    # Sort candidates by score descending (if present), else chronological
    all_clips.sort(key=lambda c: (float(c.get("score", 0)), -c.get("start", 0)), reverse=True)
    selected = all_clips[:count]
    # Re-order chronologically by start time for consistent viewing
    selected.sort(key=lambda c: c["start"])
    return selected


# ─── Second pass: comparative re-ranking ────────────────────────────────────

def rerank_clips(
    clips: list[dict],
    filter_mode: str = FilterMode.VIRAL_HOOKS,
    detected_lang: str = "uk",
    client=None,
) -> list[dict]:
    """
    Second Gemini pass: shows the model all final clips side by side and asks for a comparative
    0–100 score, so the ranking reflects how clips compare to each other instead of each being
    judged alone. The final score blends this with the rubric score. Any failure keeps the
    original scores (the clips themselves are never lost here).
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
        for model_name in CANDIDATE_MODELS:
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=_json_config(_RERANK_SCHEMA, temperature=0.2),
                )
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
