"""
title_ideas.py — three hook titles for a clip's 9:16 title plate, asked from Gemini on demand.

The author picks one (or writes their own); the picks are stored (db.title_choices) and shown to
the model next time, so the offered titles drift towards the author's taste. Nothing is invented
when Gemini fails: the caller gets TitleIdeasError with a plain-language message.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Sequence

from ai_selector import _gemini_error_kind, _strip_fences
from config_loader import CANDIDATE_MODELS, get_gemini_client
from languages import language_name

log = logging.getLogger("shorts_core")

KINDS = ("шок", "питання", "інтрига")
MAX_LEN = 80  # characters: the plate holds two short lines at most
MAX_CLIP_TEXT = 3000

QUOTA_MESSAGE = (
    "Ліміт безкоштовного ключа Gemini на зараз вичерпано, тому нових заголовків немає. "
    "Спробуй за годину або напиши свій заголовок."
)
BAD_KEY_MESSAGE = "Google не приймає ключ Gemini. Перевір його в «Налаштування → Ключ і розпізнавання»."
BUSY_MESSAGE = "Gemini зараз не відповідає. Спробуй ще раз за хвилину або напиши свій заголовок."


class TitleIdeasError(RuntimeError):
    """No titles: the message is shown to the user as is."""


def build_prompt(clip_text: str, current_title: str, lang_code: str, taste: Optional[Dict[str, Any]] = None,
                 why: str = "") -> str:
    lang = language_name(lang_code)
    lines = [
        "Ти — автор вірусних Shorts/Reels/TikTok з мільйонними переглядами. Твоє завдання: заголовок-плашка,",
        "який за 1 секунду зупиняє палець, що гортає стрічку. Ось що говорять у кліпі:",
        f"«{clip_text[:MAX_CLIP_TEXT]}»",
        f"Робоча назва кліпу: {current_title}",
    ]
    if why:
        lines.append(f"Чому цей момент обрали: {why}")
    lines += [
        "",
        "Як пишуть сильні гачки:",
        "- Відкрита петля: натякни на найсоковитіше в кліпі, але НЕ розкривай відповідь. Глядач має дивитися, щоб дізнатися.",
        "- Емоція: шок, обурення, сміх, заздрість, страх пропустити. Нейтральний опис теми — провал.",
        "- Конкретика з кліпу: гучне слово, ім'я, цифра, дивна деталь або коротка цитата найгострішої фрази.",
        "- Конфлікт чи протиставлення: «усі думають так, а він…», «сказав це прямо зі сцени».",
        "- Жива розмовна мова, теперішній час, можна звертатися на «ти». 3–8 слів.",
        "- Сміливо й гостро, на межі, але без брехні: кожне твердження має спиратися на сказане в кліпі.",
        "Заборонено: заголовок-тема («Євреї та керівництво України», «Часник і глисти»), канцелярит,",
        "слова «цікаво», «розповідає», «історія про», «про те, як», переказ назви кліпу іншими словами.",
        "",
        "Приклади (на чужих темах), слабкий → сильний:",
        "- «Як заощаджувати гроші» → «Я щомісяця викидав 3000 і не помічав»",
        "- «Комік про свою маму» → «Мама досі не знає, що я це сказав»",
        "- «Лікар про сон» → «Ти спиш правильно? Майже ніхто ні»",
        "",
        "Спершу подумки напиши 10 варіантів, потім обери 3 найсильніші, рівно по одному кожного виду:",
        "- \"шок\": смілива заява, провокація або найгостріша цитата з кліпу;",
        "- \"питання\": питання, яке чіпляє особисто глядача і на яке хочеться почути відповідь;",
        "- \"інтрига\": недомовленість, через яку неможливо не додивитися до кінця.",
        f"До {MAX_LEN} знаків кожен. Без лапок, без емодзі, без хештегів.",
        f"Мова заголовків — мова кліпу ({lang}).",
    ]
    taste = taste or {}
    recent = [r for r in taste.get("recent", []) if r.get("text")]
    if recent:
        lines.append("")
        lines.append("Заголовки, які автор раніше обрав для своїх кліпів (орієнтуйся на їхній стиль і довжину):")
        lines += [f"- [{r.get('kind', 'свій')}] {r['text']}" for r in recent[:8]]
        counts = {k: v for k, v in (taste.get("counts") or {}).items() if v and k in KINDS}
        if counts:
            fav = max(counts, key=counts.get)
            lines.append(f"Найчастіше автор обирає вид «{fav}»: зроби цей варіант особливо сильним.")
    lines += [
        "",
        'Поверни ВИКЛЮЧНО JSON масив: [{"kind": "шок", "text": "..."}, {"kind": "питання", "text": "..."}, {"kind": "інтрига", "text": "..."}]',
    ]
    return "\n".join(lines)


def parse_variants(raw: str) -> List[Dict[str, str]]:
    """Model answer → up to 3 clean variants, one per kind, in the order цифра, питання, інтрига."""
    data = json.loads(_strip_fences((raw or "").strip()))
    if isinstance(data, dict):
        data = next((v for v in data.values() if isinstance(v, list)), [])
    by_kind: Dict[str, str] = {}
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind", "")).strip().lower()
        text = re.sub(r"\s+", " ", str(item.get("text", ""))).strip().strip("\"«»'").strip()
        if kind in KINDS and text and kind not in by_kind:
            by_kind[kind] = text[:MAX_LEN].rstrip()
    return [{"kind": k, "text": by_kind[k]} for k in KINDS if k in by_kind]


def clip_text_from_words(words: Sequence[Dict[str, Any]], start: float, end: float) -> str:
    return " ".join(
        str(w.get("word", "")).strip() for w in words
        if start - 0.05 <= float(w.get("start", -1)) <= end and str(w.get("word", "")).strip()
    )


def suggest_titles(clip_text: str, current_title: str, lang_code: str = "uk",
                   taste: Optional[Dict[str, Any]] = None, client=None, why: str = "") -> List[Dict[str, str]]:
    """Asks the candidate models in turn (one pass, no long waits: the user is looking at the screen)."""
    if not clip_text.strip():
        raise TitleIdeasError("У кліпі немає тексту, тому ШІ нема з чого робити заголовок. Напиши свій.")
    from google.genai import types
    if client is None:
        try:
            client = get_gemini_client()
        except Exception as e:  # no key yet
            log.warning(f"Title ideas: no Gemini client ({e})")
            raise TitleIdeasError(BAD_KEY_MESSAGE) from e
    prompt = build_prompt(clip_text, current_title, lang_code, taste, why)
    kinds: List[str] = []
    for model_name in CANDIDATE_MODELS:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(response_mime_type="application/json", temperature=1.0,
                                                   automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)),
            )
            variants = parse_variants(response.text or "")
            if len(variants) >= 2:
                log.info(f"Title ideas from {model_name}: {len(variants)}")
                return variants
            kinds.append("other")
        except Exception as e:
            kind = _gemini_error_kind(str(e))
            kinds.append(kind)
            log.warning(f"Title ideas: {model_name} failed ({e})")
            if kind == "bad_key":
                break
    if kinds and all(k == "bad_key" for k in kinds):
        raise TitleIdeasError(BAD_KEY_MESSAGE)
    if kinds and all(k == "quota" for k in kinds):
        raise TitleIdeasError(QUOTA_MESSAGE)
    raise TitleIdeasError(BUSY_MESSAGE)
