"""
constants.py — Centralized constants, enums, and resolvers for AI Video Moments Analyzer Studio.
"""
from enum import StrEnum


class FilterMode(StrEnum):
    VIRAL_HOOKS = "Вірусні гачки (Viral Hooks)"
    ACTIONABLE_ADVICE = "Експертні поради (Value/Education)"
    STORYTELLING = "Емоційні / Історії (Storytelling)"


FILTER_INSTRUCTIONS: dict[str, str] = {
    FilterMode.VIRAL_HOOKS: (
        "Шукай найбільш вірусні, гострі моменти з потужним хуком у перші 3-5 секунд: "
        "інтрига, провокаційне твердження, несподіваний парадокс або емоційний сплеск."
    ),
    FilterMode.ACTIONABLE_ADVICE: (
        "Шукай чіткі практичні поради за структурою «Проблема → Рішення → Практичний крок»: "
        "максимум конкретики, цифр, алгоритмів дій або інсайтів без зайвої води."
    ),
    FilterMode.STORYTELLING: (
        "Шукай цілісні драматичні або надихаючі історії: "
        "чітка зав'язка, розвиток подій, несподіваний поворот та емоційний фінал."
    ),
}

FILTER_MODES: list[str] = [m.value for m in FilterMode]

def resolve_filter_mode(val: str | None) -> FilterMode:
    """Resolves CLI/UI alias or raw string to a canonical FilterMode."""
    if not val:
        return FilterMode.VIRAL_HOOKS
    s = str(val).strip().lower()
    for mode in FilterMode:
        if s == mode.value.lower() or s == mode.name.lower():
            return mode
    if any(k in s for k in ("advice", "value", "поради", "експерт")):
        return FilterMode.ACTIONABLE_ADVICE
    if any(k in s for k in ("story", "історі", "емоці")):
        return FilterMode.STORYTELLING
    return FilterMode.VIRAL_HOOKS
