"""
languages.py — video languages the studio offers (Whisper code → names shown to people and to Gemini).

"auto" lets Whisper detect the language from the first ~30 s of speech.
Any other Whisper language still works through "auto"; the list is just what the menus show.
"""
from __future__ import annotations

AUTO = "auto"

# code: (label in menus, name used inside the Gemini prompt — Ukrainian, lower case)
LANGUAGES: dict[str, tuple[str, str]] = {
    "uk": ("Українська", "українська"),
    "en": ("Англійська", "англійська"),
    "pl": ("Польська", "польська"),
    "de": ("Німецька", "німецька"),
    "fr": ("Французька", "французька"),
    "es": ("Іспанська", "іспанська"),
    "it": ("Італійська", "італійська"),
    "pt": ("Португальська", "португальська"),
}

# Short spelling hints for Whisper; helps punctuation and keeps the right alphabet
INITIAL_PROMPTS: dict[str, str] = {
    "uk": (
        "Відео записано українською мовою. Офіційний правопис, "
        "розділові знаки, крапки, коми, чиста українська лексика без суржику та русизмів."
    ),
}


def language_choices() -> list[dict[str, str]]:
    """Options for the menus: Ukrainian first (the default), then auto, then the rest."""
    out = [{"value": "uk", "label": LANGUAGES["uk"][0]}, {"value": AUTO, "label": "Автоматично"}]
    out += [{"value": code, "label": label} for code, (label, _) in LANGUAGES.items() if code != "uk"]
    return out


def is_supported(code: str) -> bool:
    return code == AUTO or code in LANGUAGES


def language_label(code: str) -> str:
    """Name for people: 'англійська'; unknown codes pass through as is."""
    code = (code or "").strip().lower()
    return LANGUAGES[code][1] if code in LANGUAGES else (code or "невідома")


def language_name(code: str) -> str:
    """Name for the Gemini prompt: 'англійська (en)'; unknown codes pass through as is."""
    code = (code or "").strip().lower()
    if code in LANGUAGES:
        return f"{LANGUAGES[code][1]} ({code})"
    return code or "невідома"
