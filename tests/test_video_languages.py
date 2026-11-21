"""
tests/test_video_languages.py — videos in other languages: language choice per video, Whisper
auto-detection, a transcript cache that respects the language, and Gemini prompts in the video's language.
"""
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from backend import db, job_runner, queue_runner, server
from languages import is_supported, language_choices, language_name
from transcription import load_transcript_cache_for, save_transcript_cache, transcribe_video

client = TestClient(server.app)


# ─── languages.py ─────────────────────────────────────────────────────────────

def test_language_choices_start_with_ukrainian_then_auto():
    values = [c["value"] for c in language_choices()]
    assert values[:3] == ["uk", "auto", "en"]
    assert all(is_supported(v) for v in values)
    assert not is_supported("xx") and not is_supported("")


def test_language_name_for_prompt():
    assert language_name("en") == "англійська (en)"
    assert language_name("UK") == "українська (uk)"
    assert language_name("ja") == "ja"          # any Whisper language still passes through


# ─── transcript cache respects the language choice ────────────────────────────

@pytest.fixture(autouse=True)
def _cache_in_tmp(tmp_path, monkeypatch):
    from config_loader import CONFIG
    monkeypatch.setitem(CONFIG["cache"], "dir", str(tmp_path / "cache"))
    monkeypatch.setitem(CONFIG["cache"], "enabled", True)


def _video(tmp_path, name="v.mp4"):
    p = tmp_path / name
    p.write_bytes(name.encode() * 100)
    return str(p)


def test_cache_made_for_ukrainian_is_not_reused_for_english(tmp_path):
    path = _video(tmp_path)
    save_transcript_cache(path, [{"start": 0, "end": 1, "text": "привіт"}], [], "uk", requested="uk")
    assert load_transcript_cache_for(path, "uk") is not None
    assert load_transcript_cache_for(path, "en") is None
    assert load_transcript_cache_for(path, "auto") is None     # was forced to uk, maybe wrongly


def test_cache_made_by_auto_detection_serves_auto_and_the_detected_language(tmp_path):
    path = _video(tmp_path)
    save_transcript_cache(path, [{"start": 0, "end": 1, "text": "hello"}], [], "en", requested="auto")
    assert load_transcript_cache_for(path, "auto")[2] == "en"
    assert load_transcript_cache_for(path, "en") is not None
    assert load_transcript_cache_for(path, "uk") is None


def test_old_cache_without_requested_field_still_works(tmp_path):
    path = _video(tmp_path)
    save_transcript_cache(path, [{"start": 0, "end": 1, "text": "привіт"}], [], "uk")
    assert load_transcript_cache_for(path, "uk") is not None


# ─── Whisper auto-detection ───────────────────────────────────────────────────

def _whisper(lang):
    m = MagicMock()
    seg = MagicMock(start=0.0, end=5.0, text="text", words=[])
    m.transcribe.return_value = ([seg], MagicMock(language=lang, duration=5.0, language_probability=0.9))
    return m


@patch("transcription.load_transcript_cache_for", return_value=None)
@patch("transcription.extract_audio")
@patch("transcription.save_transcript_cache")
def test_auto_detected_ukrainian_restarts_with_spelling_hint(mock_save, _ex, _cache):
    model = _whisper("uk")
    msgs = []
    with patch("transcription.get_whisper", return_value=model):
        _, _, lang, _ = transcribe_video("v.mp4", status_cb=msgs.append, language="auto")
    assert lang == "uk"
    first, second = model.transcribe.call_args_list
    assert "language" not in first.kwargs and "initial_prompt" not in first.kwargs
    assert second.kwargs["language"] == "uk" and "українською" in second.kwargs["initial_prompt"]
    assert any("Мова відео: українська" in str(m) for m in msgs)
    assert mock_save.call_args.kwargs["requested"] == "auto"


@patch("transcription.load_transcript_cache_for", return_value=None)
@patch("transcription.extract_audio")
@patch("transcription.save_transcript_cache")
def test_auto_detected_english_runs_once_without_hint(_save, _ex, _cache):
    model = _whisper("en")
    with patch("transcription.get_whisper", return_value=model):
        _, _, lang, _ = transcribe_video("v.mp4", language="auto")
    assert lang == "en"
    assert model.transcribe.call_count == 1
    assert "initial_prompt" not in model.transcribe.call_args.kwargs


# ─── Gemini prompt in the video's language ────────────────────────────────────

def _prompt_for(lang):
    from ai_selector import _call_gemini_single_window

    gemini = MagicMock()
    gemini.models.generate_content.return_value = MagicMock(text='[{"start": 0, "end": 20, "title": "T"}]')
    _call_gemini_single_window(
        client=gemini, segments=[{"start": 0, "end": 20, "text": "hello"}], count=1,
        min_len=15, max_len=60, filter_mode="Viral Hooks", detected_lang=lang,
    )
    return gemini.models.generate_content.call_args.kwargs["contents"]


def test_english_video_prompt_asks_for_english_texts_and_ukrainian_reason():
    prompt = _prompt_for("en")
    assert "англійська (en)" in prompt
    assert "МОВОЮ ВІДЕО" in prompt and "'reason' пиши українською" in prompt


def test_ukrainian_video_prompt_keeps_everything_ukrainian():
    prompt = _prompt_for("uk")
    assert "українська (uk)" in prompt
    assert "Усі тексти" in prompt and "МОВОЮ ВІДЕО" not in prompt


# ─── language per video through the API and the queue ─────────────────────────

def _project(pid):
    src = server.UPLOADS_DIR / f"{pid}.mp4"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"\0" * 2048)
    return db.create_project(pid, f"{pid}.mp4", str(src), 300.0)


def test_queue_passes_language_to_analysis_and_keeps_it_on_retry():
    _project("proj_en")
    r = client.post("/api/queue", json={"project_ids": ["proj_en"], "language": "EN"})
    assert r.status_code == 200 and r.json()["added"][0]["language"] == "en"

    seen = []

    def fake(job_id, project_id, clips_count, filter_mode, engine="whisper_local", language=None):
        seen.append(language)
        job_runner.broadcast_update(job_id, {"status": "failed", "progress": 1.0, "error": "x", "message": "x"})

    with patch.object(job_runner, "_run_analyze_worker", fake):
        failed = queue_runner.process_next()
    assert seen == ["en"]
    new = client.post(f"/api/queue/{failed['id']}/retry").json()["item"]
    assert new["language"] == "en"


def test_queue_without_language_uses_settings():
    _project("proj_def")
    client.post("/api/queue", json={"project_ids": ["proj_def"]})
    seen = []
    with patch.object(job_runner, "_run_analyze_worker",
                      lambda *a, language=None, **k: seen.append(language)):
        queue_runner.process_next()
    assert seen == [None]


def test_unknown_language_is_rejected():
    _project("proj_bad")
    assert client.post("/api/queue", json={"project_ids": ["proj_bad"], "language": "klingon"}).status_code == 422
    assert client.post("/api/settings", json={"whisper": {"language": "klingon"}}).status_code == 400
    r = client.post("/api/settings", json={"whisper": {"language": "de"}})
    assert r.status_code == 200 and r.json()["values"]["whisper"]["language"] == "de"
    assert "de" in [c["value"] for c in r.json()["choices"]["language"]]
