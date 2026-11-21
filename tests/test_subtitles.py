"""
tests/test_subtitles.py — Unit tests for subtitles_generator.py.
Verifies SRT formatting, word chunking for Shorts/CapCut, relative zero-based timecodes,
segment fallback, file writing, and ZIP bundling.
"""
import os
import zipfile
import pytest

from subtitles_generator import (
    format_srt_timestamp,
    _chunk_words_for_shorts,
    generate_srt_for_clip,
    write_clip_srt_file,
    create_all_subtitles_zip,
)


def test_format_srt_timestamp():
    assert format_srt_timestamp(0.0) == "00:00:00,000"
    assert format_srt_timestamp(1.234) == "00:00:01,234"
    assert format_srt_timestamp(65.5) == "00:01:05,500"
    assert format_srt_timestamp(3661.05) == "01:01:01,050"
    assert format_srt_timestamp(-5.0) == "00:00:00,000"


def test_chunk_words_for_shorts():
    words = [
        {"start": 10.0, "end": 10.4, "word": "Це"},
        {"start": 10.5, "end": 10.9, "word": "перший"},
        {"start": 11.0, "end": 11.5, "word": "фрагмент!"},
        {"start": 11.6, "end": 12.0, "word": "А"},
        {"start": 12.1, "end": 12.5, "word": "це"},
        {"start": 12.6, "end": 13.0, "word": "продовження"},
        {"start": 13.1, "end": 13.5, "word": "думки."},
    ]
    chunks = _chunk_words_for_shorts(words, target_words_per_cue=3, max_words_per_cue=4)
    assert len(chunks) == 2
    assert [w["word"] for w in chunks[0]] == ["Це", "перший", "фрагмент!"]
    assert [w["word"] for w in chunks[1]] == ["А", "це", "продовження", "думки."]


def test_generate_srt_for_clip_from_cached_words(tmp_path, monkeypatch):
    mock_words = [
        {"start": 20.0, "end": 20.5, "word": "Головна"},
        {"start": 20.6, "end": 21.0, "word": "ідея"},
        {"start": 21.1, "end": 21.8, "word": "відео."},
        {"start": 22.0, "end": 22.5, "word": "Слухайте"},
        {"start": 22.6, "end": 23.0, "word": "уважно!"},
    ]
    mock_segments = [{"start": 20.0, "end": 23.0, "text": "Головна ідея відео. Слухайте уважно!"}]

    monkeypatch.setattr(
        "subtitles_generator.load_transcript_cache",
        lambda p, **kw: (mock_segments, mock_words, "uk"),
    )

    srt = generate_srt_for_clip("dummy_vid.mp4", start_sec=20.0, end_sec=23.0)
    assert "1\n00:00:00,000 --> 00:00:01,800\nГоловна ідея відео." in srt
    assert "2\n00:00:02,000 --> 00:00:03,000\nСлухайте уважно!" in srt


def test_generate_srt_for_clip_fallback_segments(monkeypatch):
    # Words list is empty, should fall back to segment interpolation
    mock_segments = [
        {"start": 10.0, "end": 20.0, "text": "Один два три чотири п'ять шість"}
    ]
    monkeypatch.setattr(
        "subtitles_generator.load_transcript_cache",
        lambda p, **kw: (mock_segments, [], "uk"),
    )

    srt = generate_srt_for_clip("dummy_vid.mp4", start_sec=10.0, end_sec=20.0)
    assert "1\n00:00:00,000 -->" in srt
    assert "Один два три" in srt
    assert "чотири п'ять шість" in srt


def test_generate_srt_for_clip_empty_cache_fallback(monkeypatch):
    monkeypatch.setattr("subtitles_generator.load_transcript_cache", lambda p, **kw: None)

    srt = generate_srt_for_clip("dummy_vid.mp4", start_sec=0.0, end_sec=15.0)
    assert "1\n00:00:00,000 --> 00:00:05,000\n[Фрагмент відео]" in srt


def test_write_clip_srt_file(tmp_path, monkeypatch):
    monkeypatch.setattr("subtitles_generator.load_transcript_cache", lambda p, **kw: None)
    out_file = tmp_path / "subs" / "clip_1.srt"

    res = write_clip_srt_file("test.mp4", start_sec=5.0, end_sec=25.0, output_path=str(out_file))
    assert os.path.exists(res)
    content = out_file.read_text(encoding="utf-8")
    assert "[Фрагмент відео]" in content


def test_create_all_subtitles_zip(tmp_path, monkeypatch):
    mock_words = [
        {"start": 0.0, "end": 1.0, "word": "Привіт"},
        {"start": 1.0, "end": 2.0, "word": "світ"},
    ]
    monkeypatch.setattr(
        "subtitles_generator.load_transcript_cache",
        lambda p, **kw: ([], mock_words, "uk"),
    )

    clips = [
        {"clip_index": 1, "start_sec": 0.0, "end_sec": 10.0, "title": "Перший кліп"},
        {"clip_index": 2, "start_sec": 10.0, "end_sec": 20.0, "title": "Другий кліп"},
    ]
    out_zip = tmp_path / "all_subs.zip"

    res = create_all_subtitles_zip(
        project_id="test_proj",
        project_title="Тестове відео",
        source_path="video.mp4",
        clips=clips,
        output_zip_path=str(out_zip),
    )

    assert os.path.exists(res)
    with zipfile.ZipFile(res, "r") as zf:
        namelist = zf.namelist()
        assert len(namelist) == 2
        assert any("clip_01" in n for n in namelist)
        assert any("clip_02" in n for n in namelist)
