"""
tests/test_transcription.py — Unit tests for Whisper transcription, SHA-256 caching, and pipeline in transcription.py.
"""
import json
import tempfile
from unittest.mock import MagicMock, patch

import pytest
from transcription import (
    _cache_path,
    load_transcript_cache,
    save_transcript_cache,
    transcribe_video,
)


def test_cache_path_sha256_hashing():
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(b"sample_video_bytes_for_hashing")
        path = f.name
    try:
        p = _cache_path(path)
        assert p.suffix == ".json"
        digest = p.stem.split("_")[0]
        assert len(digest) == 64  # SHA-256 hex string is exactly 64 characters
        # A model other than "small" is part of the name, so switching models never reuses a weaker transcript
        from config_loader import CONFIG
        model = CONFIG["whisper"]["model"]
        assert p.stem == digest if model == "small" else p.stem.endswith(model)
    finally:
        import os
        os.unlink(path)


def test_save_and_load_cache():
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as f:
        f.write(b"unique_cache_test_bytes")
        path = f.name
    try:
        segments = [{"start": 0.0, "end": 5.0, "text": "hello"}]
        words = [{"start": 0.0, "end": 1.0, "word": "hello"}]
        save_transcript_cache(path, segments, words, "uk")

        cached = load_transcript_cache(path)
        assert cached is not None
        cached_segs, cached_words, cached_lang = cached
        assert len(cached_segs) == 1
        assert cached_segs[0]["text"] == "hello"
        assert cached_lang == "uk"
    finally:
        import os
        os.unlink(path)


def test_load_cache_miss_returns_none():
    assert load_transcript_cache("non_existent_file_xyz_123.mp4") is None


@patch("transcription.load_transcript_cache_for")
def test_transcribe_video_returns_cached_if_available(mock_cache):
    mock_cache.return_value = ([{"start": 0, "end": 5, "text": "cached"}], [], "en")
    status_calls = []

    segs, words, lang, from_cache = transcribe_video("video.mp4", status_cb=status_calls.append)
    assert from_cache is True
    assert segs[0]["text"] == "cached"
    assert lang == "en"
    assert len(status_calls) == 1


@patch("transcription.load_transcript_cache_for", return_value=None)
@patch("transcription.extract_audio")
@patch("transcription.get_whisper")
@patch("transcription.save_transcript_cache")
def test_transcribe_video_runs_whisper_on_cache_miss(
    mock_save, mock_get_whisper, mock_extract, _mock_cache
):
    mock_whisper = MagicMock()
    # Mock whisper segment and info
    mock_seg = MagicMock()
    mock_seg.start = 1.0
    mock_seg.end = 4.0
    mock_seg.text = "whisper text"
    mock_seg.words = []

    mock_info = MagicMock()
    mock_info.language = "uk"

    mock_whisper.transcribe.return_value = ([mock_seg], mock_info)
    mock_get_whisper.return_value = mock_whisper

    status_msgs = []
    segs, words, lang, from_cache = transcribe_video("video.mp4", status_cb=status_msgs.append)
    assert from_cache is False
    assert len(segs) == 1
    assert segs[0]["text"] == "whisper text"
    assert lang == "uk"
    assert mock_extract.called
    assert mock_save.called

    call_kwargs = mock_whisper.transcribe.call_args[1]
    assert call_kwargs.get("vad_filter") is True
    assert "українською мовою" in call_kwargs.get("initial_prompt", "")


@patch("transcription.load_transcript_cache_for", return_value=None)
@patch("transcription.extract_audio")
@patch("transcription.get_whisper")
@patch("transcription.save_transcript_cache")
def test_transcribe_video_openai_whisper_engine(
    mock_save, mock_get_whisper, mock_extract, _mock_cache
):
    from state import app_state
    mock_whisper = MagicMock()
    mock_whisper.transcribe.return_value = {
        "language": "uk",
        "segments": [
            {
                "start": 0.5,
                "end": 2.5,
                "text": "тестовий текст",
                "words": [{"start": 0.5, "end": 2.5, "word": "тестовий"}],
            }
        ],
    }
    mock_get_whisper.return_value = mock_whisper

    prev_engine = getattr(app_state, "_whisper_engine", "faster_whisper")
    try:
        app_state._whisper_engine = "openai_whisper"
        segs, words, lang, from_cache = transcribe_video("video.mp4")
        assert from_cache is False
        assert len(segs) == 1
        assert segs[0]["text"] == "тестовий текст"
        assert len(words) == 1
        assert words[0]["word"] == "тестовий"
        assert lang == "uk"
    finally:
        app_state._whisper_engine = prev_engine


@patch("transcription.load_transcript_cache_for", return_value=None)
@patch("transcription.extract_audio")
@patch("transcription.get_whisper")
@patch("transcription.save_transcript_cache")
def test_transcribe_video_omits_uk_prompt_for_non_uk_languages(
    mock_save, mock_get_whisper, mock_extract, _mock_cache
):
    """BUG-05: Ukrainian initial_prompt must NOT be sent when language is 'en' or 'auto'."""
    mock_whisper = MagicMock()
    mock_seg = MagicMock(start=0.0, end=5.0, text="Hello world", words=[])
    mock_info = MagicMock(language="en", duration=5.0)
    mock_whisper.transcribe.return_value = ([mock_seg], mock_info)
    mock_get_whisper.return_value = mock_whisper

    segs, _, lang, _ = transcribe_video("english.mp4", language="en")
    assert lang == "en"
    assert len(segs) == 1
    call_kwargs = mock_whisper.transcribe.call_args.kwargs
    assert "initial_prompt" not in call_kwargs
    assert call_kwargs["language"] == "en"


@patch("transcription.load_transcript_cache_for", return_value=None)
@patch("transcription.extract_audio")
@patch("transcription.get_whisper")
@patch("transcription.save_transcript_cache")
@patch("faster_whisper.WhisperModel")
def test_transcribe_video_caches_cpu_fallback_in_app_state(
    mock_whisper_cls, mock_save, mock_get_whisper, mock_extract, _mock_cache
):
    """BUG-05: When GPU Whisper fails at runtime, CPU fallback model must be cached in AppState."""
    from state import app_state

    failing_gpu_model = MagicMock()
    failing_gpu_model.transcribe.side_effect = RuntimeError("cublas64_12.dll not found")
    mock_get_whisper.return_value = failing_gpu_model

    cpu_model = MagicMock()
    mock_seg = MagicMock(start=0.0, end=3.0, text="Fallback ok", words=[])
    mock_info = MagicMock(language="uk", duration=3.0)
    cpu_model.transcribe.return_value = ([mock_seg], mock_info)
    mock_whisper_cls.return_value = cpu_model

    prev_whisper = app_state._whisper
    try:
        segs, _, lang, _ = transcribe_video("video.mp4")
        assert len(segs) == 1
        assert segs[0]["text"] == "Fallback ok"
        assert app_state._whisper is cpu_model
    finally:
        app_state._whisper = prev_whisper
