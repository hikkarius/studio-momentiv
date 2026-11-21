"""
tests/test_constants.py — Unit tests for constants.py enums, mappings, and resolvers.
"""
from constants import (
    FILTER_INSTRUCTIONS,
    FILTER_MODES,
    LANGUAGE_CHOICES,
    LANGUAGE_CODE_MAP,
    MAX_CLIP_DURATION,
    MIN_CLIP_DURATION,
    FilterMode,
    VideoLanguage,
    resolve_filter_mode,
)


def test_filter_mode_instructions():
    assert len(FilterMode) == 3
    assert len(FILTER_MODES) == 3
    for mode in FilterMode:
        assert mode in FILTER_INSTRUCTIONS
        assert len(FILTER_INSTRUCTIONS[mode]) > 10


def test_video_language_mappings():
    assert len(LANGUAGE_CHOICES) == len(VideoLanguage)
    assert LANGUAGE_CODE_MAP[VideoLanguage.UKRAINIAN] == "uk"
    assert LANGUAGE_CODE_MAP[VideoLanguage.AUTO] == "auto"
    assert LANGUAGE_CODE_MAP[VideoLanguage.ENGLISH] == "en"
    assert MIN_CLIP_DURATION < MAX_CLIP_DURATION


def test_resolve_filter_mode():
    assert resolve_filter_mode(None) == FilterMode.VIRAL_HOOKS
    assert resolve_filter_mode("") == FilterMode.VIRAL_HOOKS
    assert resolve_filter_mode(FilterMode.STORYTELLING.value) == FilterMode.STORYTELLING
    assert resolve_filter_mode("ACTIONABLE_ADVICE") == FilterMode.ACTIONABLE_ADVICE
    assert resolve_filter_mode("Actionable Advice") == FilterMode.ACTIONABLE_ADVICE
    assert resolve_filter_mode("advice") == FilterMode.ACTIONABLE_ADVICE
    assert resolve_filter_mode("експертні поради") == FilterMode.ACTIONABLE_ADVICE
    assert resolve_filter_mode("Storytelling") == FilterMode.STORYTELLING
    assert resolve_filter_mode("історія") == FilterMode.STORYTELLING
    assert resolve_filter_mode("Viral Hooks") == FilterMode.VIRAL_HOOKS
    assert resolve_filter_mode("unknown_style") == FilterMode.VIRAL_HOOKS
