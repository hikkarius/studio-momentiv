"""
tests/test_ai_selector.py — Unit tests for AI clip selection, context protection, and chunking (R-6).
"""
from unittest.mock import MagicMock, patch

import pytest
from ai_selector import (
    _MAX_TRANSCRIPT_CHARS,
    GeminiUnavailableError,
    _parse_clips_json,
    analyze_and_find_clips,
    validate_clips,
)
from config_loader import CANDIDATE_MODELS


def test_parse_clips_json_fences():
    text = '```json\n[{"start": 10.0, "end": 30.0, "title": "TEST", "score": 90}]\n```'
    clips = _parse_clips_json(text)
    assert len(clips) == 1
    assert clips[0]["title"] == "TEST"


def test_parse_clips_json_wrapped_dict():
    text = '{"highlights": [{"start": 5.0, "end": 25.0, "title": "WRAP", "score": 85}]}'
    clips = _parse_clips_json(text)
    assert len(clips) == 1
    assert clips[0]["title"] == "WRAP"


def test_parse_clips_json_invalid_raises():
    with pytest.raises(RuntimeError):
        _parse_clips_json("This is not JSON at all")


def test_empty_segments_returns_empty_list():
    assert analyze_and_find_clips(client=MagicMock(), segments=[]) == []


def test_large_transcript_smart_chunking():
    """Simulates a 3-hour video with >100,000 characters of transcript."""
    # Create segments that exceed _MAX_TRANSCRIPT_CHARS
    sample_text = "Це довгий текст транскрипції, який повторюється для симуляції великого відеоподкасту. " * 5
    segments = [
        {"start": i * 30, "end": (i + 1) * 30, "text": f"{sample_text} Segment #{i}"}
        for i in range(300)
    ]

    mock_client = MagicMock()
    mock_resp = MagicMock()
    # Mock model response returning 2 clips per window
    mock_resp.text = '[{"start": 10.0, "end": 35.0, "title": "CLIP A", "score": 95}, {"start": 50.0, "end": 80.0, "title": "CLIP B", "score": 80}]'
    mock_client.models.generate_content.return_value = mock_resp

    clips = analyze_and_find_clips(
        client=mock_client,
        segments=segments,
        count=3,
    )

    # Must call generate_content more than once (proving chunking across multiple windows)
    assert mock_client.models.generate_content.call_count >= 2
    # Must return exactly the requested count
    assert len(clips) == 3


@pytest.fixture
def no_sleep(monkeypatch):
    """Records retry back-off pauses instead of really sleeping."""
    sleeps: list[float] = []
    monkeypatch.setattr("ai_selector.time.sleep", lambda s: sleeps.append(s))
    return sleeps


def _resp(text):
    r = MagicMock()
    r.text = text
    return r


def test_all_models_503_raises_gemini_unavailable_without_fake_clips(no_sleep):
    segments = [
        {"start": 0.0, "end": 15.0, "text": "Вступна частина відео"},
        {"start": 15.0, "end": 35.0, "text": "Головна ідея та інсайти"},
        {"start": 35.0, "end": 55.0, "text": "Висновки та підсумки"},
    ]
    mock_client = MagicMock()
    # Simulate all Gemini models throwing high demand 503 errors
    mock_client.models.generate_content.side_effect = Exception("503 UNAVAILABLE - High demand")

    with pytest.raises(GeminiUnavailableError) as exc:
        analyze_and_find_clips(
            client=mock_client,
            segments=segments,
            count=2,
            min_len=15,
            max_len=30,
        )
    assert "перевантажений" in str(exc.value)
    # at most 4 requests per window (the models in turn, a pause when the list starts over), one try of
    # the fallback model, and no second round when Google answered nothing at all
    from ai_selector import MAX_ATTEMPTS_PER_WINDOW
    assert mock_client.models.generate_content.call_count == MAX_ATTEMPTS_PER_WINDOW + 1
    assert no_sleep == [5.0] * (MAX_ATTEMPTS_PER_WINDOW // len(CANDIDATE_MODELS) - 1)


def test_recovers_on_second_pass(no_sleep):
    segments = [{"start": 0.0, "end": 60.0, "text": "Текст"}]
    mock_client = MagicMock()
    n_models = len(CANDIDATE_MODELS)
    good = _resp('[{"start": 5.0, "end": 35.0, "title": "ОК", "score": 90}]')
    mock_client.models.generate_content.side_effect = (
        [Exception("503 UNAVAILABLE")] * n_models + [good]
    )

    clips = analyze_and_find_clips(client=mock_client, segments=segments, count=1)
    assert len(clips) == 1
    assert clips[0]["title"] == "ОК"
    assert no_sleep == [5.0]


def test_json_parse_failure_tries_next_model(no_sleep):
    segments = [{"start": 0.0, "end": 60.0, "text": "Текст"}]
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = [
        _resp("це не JSON"),
        _resp('[{"start": 1.0, "end": 30.0, "title": "ДРУГА МОДЕЛЬ", "score": 80}]'),
    ]
    clips = analyze_and_find_clips(client=mock_client, segments=segments, count=1)
    assert clips[0]["title"] == "ДРУГА МОДЕЛЬ"
    assert mock_client.models.generate_content.call_count == 2
    assert no_sleep == []


def _long_segments():
    sample_text = "Це довгий текст транскрипції для симуляції великого подкасту. " * 6
    return [
        {"start": i * 30, "end": (i + 1) * 30, "text": f"{sample_text} #{i}"}
        for i in range(300)
    ]


def test_chunked_keeps_clips_from_successful_windows(monkeypatch):
    import ai_selector

    calls = {"n": 0}

    def fake_window(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise GeminiUnavailableError("down")
        start = float(kwargs["segments"][0]["start"])
        return [{"start": start, "end": start + 30, "title": f"W{calls['n']}", "score": 90}]

    monkeypatch.setattr(ai_selector, "_call_gemini_single_window", fake_window)
    clips = analyze_and_find_clips(client=MagicMock(), segments=_long_segments(), count=3)
    assert calls["n"] >= 2
    assert len(clips) >= 1
    assert all(c["title"] != "W1" for c in clips)


def test_chunked_all_windows_fail_reraises(monkeypatch):
    import ai_selector

    def fake_window(**kwargs):
        raise GeminiUnavailableError(ai_selector.GEMINI_UNAVAILABLE_MESSAGE)

    monkeypatch.setattr(ai_selector, "_call_gemini_single_window", fake_window)
    with pytest.raises(GeminiUnavailableError):
        analyze_and_find_clips(client=MagicMock(), segments=_long_segments(), count=3)


def test_heuristic_fallback_removed():
    import ai_selector
    assert not hasattr(ai_selector, "_fallback_heuristic_clips")


def test_prompt_contains_length_bounds_and_viral_instruction():
    segments = [{"start": 0.0, "end": 90.0, "text": "Історія"}]
    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = _resp('[{"start": 0.0, "end": 30.0, "title": "T", "score": 90}]')
    analyze_and_find_clips(client=mock_client, segments=segments, count=1, min_len=15, max_len=60)
    prompt = mock_client.models.generate_content.call_args.kwargs["contents"]
    assert "від 15 до 60 секунд" in prompt
    assert "НАЙВІРУСНІШІ" in prompt
    assert "САМОДОСТАТНІМ" in prompt


def test_validate_clips_enforces_bounds_with_tolerance():
    clips = [
        {"start": 0.0, "end": 10.0, "title": "TOO SHORT"},     # 10s < 15-3 → dropped
        {"start": 20.0, "end": 33.0, "title": "SHORT OK"},     # 13s within tolerance → kept
        {"start": 40.0, "end": 140.0, "title": "TOO LONG"},    # 100s → trimmed to 60s
        {"start": 150.0, "end": 212.0, "title": "LONG OK"},    # 62s within tolerance → kept
    ]
    result = validate_clips(clips, 300.0, min_len=15, max_len=60)
    titles = [c["title"] for c in result]
    assert titles == ["SHORT OK", "TOO LONG", "LONG OK"]
    trimmed = result[1]
    assert trimmed["end"] - trimmed["start"] == pytest.approx(60.0)
    assert result[2]["end"] - result[2]["start"] == pytest.approx(62.0)


def test_filter_mode_alias_resolves_to_exact_instruction():
    """BUG-02: English UI filter aliases must resolve to exact Ukrainian FILTER_INSTRUCTIONS."""
    from constants import FILTER_INSTRUCTIONS, FilterMode

    segments = [{"start": 0.0, "end": 30.0, "text": "Історія про великий успіх"}]
    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.text = '[{"start": 0.0, "end": 25.0, "title": "ІСТОРІЯ", "score": 91}]'
    mock_client.models.generate_content.return_value = mock_resp

    analyze_and_find_clips(
        client=mock_client,
        segments=segments,
        count=1,
        filter_mode="Storytelling",
    )
    prompt_sent = mock_client.models.generate_content.call_args.kwargs["contents"]
    assert FILTER_INSTRUCTIONS[FilterMode.STORYTELLING] in prompt_sent

    analyze_and_find_clips(
        client=mock_client,
        segments=segments,
        count=1,
        filter_mode="Actionable Advice",
    )
    prompt_sent_2 = mock_client.models.generate_content.call_args.kwargs["contents"]
    assert FILTER_INSTRUCTIONS[FilterMode.ACTIONABLE_ADVICE] in prompt_sent_2



def test_rerank_blends_comparative_scores():
    from ai_selector import rerank_clips
    clips = [
        {"start": 0, "end": 20, "title": "A", "score": 60, "hook_text": "a", "reason": "r"},
        {"start": 30, "end": 50, "title": "B", "score": 60, "hook_text": "b", "reason": "r"},
    ]
    client = MagicMock()
    client.models.generate_content.return_value = _resp('[{"n": 1, "score": 100}, {"n": 2, "score": 20}]')
    out = rerank_clips(clips, client=client)
    assert out[0]["score"] == 80 and out[1]["score"] == 40


def test_rerank_failure_keeps_original_scores():
    from ai_selector import rerank_clips
    clips = [{"start": 0, "end": 20, "title": "A", "score": 70}, {"start": 30, "end": 50, "title": "B", "score": 65}]
    client = MagicMock()
    client.models.generate_content.side_effect = RuntimeError("503 UNAVAILABLE")
    out = rerank_clips(clips, client=client)
    assert [c["score"] for c in out] == [70, 65]


_SEGMENTS = [
    {"start": 0.0, "end": 15.0, "text": "Вступна частина відео"},
    {"start": 15.0, "end": 35.0, "text": "Головна ідея та інсайти"},
    {"start": 35.0, "end": 55.0, "text": "Висновки та підсумки"},
]


def test_free_key_quota_gets_its_own_message(no_sleep):
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = Exception(
        "429 RESOURCE_EXHAUSTED. You exceeded your current quota")
    with pytest.raises(GeminiUnavailableError) as exc:
        analyze_and_find_clips(client=mock_client, segments=_SEGMENTS, count=2, min_len=15, max_len=30)
    assert "Ліміт безкоштовного ключа" in str(exc.value)


def test_wrong_key_fails_fast_with_settings_hint(no_sleep):
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = Exception(
        "400 INVALID_ARGUMENT API key not valid. Please pass a valid API key. API_KEY_INVALID")
    with pytest.raises(GeminiUnavailableError) as exc:
        analyze_and_find_clips(client=mock_client, segments=_SEGMENTS, count=2, min_len=15, max_len=30)
    assert "Налаштування" in str(exc.value)
    assert no_sleep == []  # no pointless 25 s of retries for a wrong key


def test_mixed_errors_fall_back_to_overloaded_message(no_sleep):
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = [
        Exception("429 RESOURCE_EXHAUSTED") if i % 2 else Exception("503 UNAVAILABLE") for i in range(40)
    ]
    with pytest.raises(GeminiUnavailableError) as exc:
        analyze_and_find_clips(client=mock_client, segments=_SEGMENTS, count=2, min_len=15, max_len=30)
    assert "перевантажений" in str(exc.value)


def test_gemini_calls_request_schema_json():
    """Both Gemini calls ask for schema-checked JSON, so a broken answer can't slip through."""
    from ai_selector import _CLIPS_SCHEMA, _RERANK_SCHEMA, rerank_clips

    mock_client = MagicMock()
    mock_client.models.generate_content.return_value = _resp(
        '[{"start": 5.0, "end": 35.0, "title": "ОК", "score": 90}]'
    )
    analyze_and_find_clips(client=mock_client, segments=[{"start": 0.0, "end": 60.0, "text": "Т"}], count=1)
    cfg = mock_client.models.generate_content.call_args.kwargs["config"]
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_json_schema == _CLIPS_SCHEMA
    assert cfg.automatic_function_calling.disable is True

    mock_client.models.generate_content.return_value = _resp('[{"n": 1, "score": 90}, {"n": 2, "score": 40}]')
    rerank_clips([{"start": 0, "end": 20, "score": 60}, {"start": 30, "end": 50, "score": 60}], client=mock_client)
    assert mock_client.models.generate_content.call_args.kwargs["config"].response_json_schema == _RERANK_SCHEMA


def test_default_models_have_no_dead_alias():
    """gemini-flash-lite-latest answers 404 on API v1 (SDK >= 3), so it only wastes a try."""
    from config_schema import GeminiConfig

    assert "gemini-flash-lite-latest" not in GeminiConfig().candidate_models
    assert "gemini-flash-lite-latest" not in CANDIDATE_MODELS


def test_gemini_timeout_is_long_enough():
    """45 s cut long transcript windows with 504 DEADLINE_EXCEEDED."""
    import state

    assert state.GEMINI_TIMEOUT_MS >= 90_000
