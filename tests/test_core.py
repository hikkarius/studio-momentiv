"""
tests/test_core.py — Unit tests for core.py utilities.

Run with:
    pytest tests/ -v
"""
import json
import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from core import (
    analyze_and_find_clips,
    ensure_gemini_api_key,
    get_video_duration,
    validate_clips,
)

# ─── get_video_duration ───────────────────────────────────────────────────────

class TestGetVideoDuration:
    def test_returns_zero_for_nonexistent_file(self):
        assert get_video_duration("/nonexistent/file.mp4") == 0.0

    def test_returns_zero_for_none(self):
        assert get_video_duration(None) == 0.0

    @patch("utils.os.path.exists", return_value=True)
    @patch("utils.subprocess.run")
    def test_parses_ffprobe_output(self, mock_run, _mock_exists):
        mock_run.return_value = MagicMock(stdout="123.45\n", returncode=0)
        result = get_video_duration("/fake/video.mp4")
        assert result == pytest.approx(123.45)

    @patch("utils.os.path.exists", return_value=True)
    @patch("utils.subprocess.run", side_effect=Exception("ffprobe not found"))
    def test_returns_zero_on_ffprobe_error(self, _mock_run, _mock_exists):
        assert get_video_duration("/fake/video.mp4") == 0.0

# ─── validate_clips ───────────────────────────────────────────────────────────

class TestValidateClips:
    def test_empty_list(self):
        assert validate_clips([], 100.0, 20, 60) == []

    def test_valid_clip_passes_through(self):
        clips = [{"start": 10.0, "end": 40.0, "title": "TEST"}]
        result = validate_clips(clips, 100.0, 20, 60)
        assert len(result) == 1
        assert result[0]["start"] == 10.0
        assert result[0]["end"] == 40.0

    def test_inverted_bounds_are_swapped(self):
        clips = [{"start": 50.0, "end": 20.0, "title": "TEST"}]
        result = validate_clips(clips, 100.0, 20, 60)
        assert len(result) == 1
        assert result[0]["start"] < result[0]["end"]
        assert result[0]["start"] == pytest.approx(20.0)
        assert result[0]["end"] == pytest.approx(50.0)

    def test_too_short_clip_is_dropped(self):
        clips = [{"start": 0.0, "end": 3.0, "title": "SHORT"}]
        assert validate_clips(clips, 100.0, 20, 60) == []

    def test_end_clamped_to_video_duration(self):
        clips = [{"start": 80.0, "end": 130.0, "title": "LONG"}]
        result = validate_clips(clips, 100.0, 20, 60)
        assert result[0]["end"] <= 100.0

    def test_start_clamped_above_zero(self):
        clips = [{"start": -10.0, "end": 30.0, "title": "NEG"}]
        result = validate_clips(clips, 100.0, 20, 60)
        assert result[0]["start"] >= 0.0

# ─── Config loading ───────────────────────────────────────────────────────────

class TestConfigLoading:
    def test_config_has_required_sections(self):
        from core import CONFIG
        assert "whisper" in CONFIG
        assert "clips" in CONFIG
        assert "gemini" in CONFIG
        assert "cache" in CONFIG

    def test_candidate_models_is_nonempty_list(self):
        from core import CANDIDATE_MODELS
        assert isinstance(CANDIDATE_MODELS, list)
        assert len(CANDIDATE_MODELS) > 0

    def test_clip_interval_sec_is_positive(self):
        from core import CLIP_INTERVAL_SEC
        assert CLIP_INTERVAL_SEC > 0

# ─── ensure_gemini_api_key ───────────────────────────────────────────────────

class TestGeminiApiKey:
    def test_ensure_gemini_api_key_returns_key(self):
        # The key from the (isolated) .env is found and put into the environment.
        import config_loader
        fake_key = "AIza" + "x" * 35
        config_loader.ENV_PATH.write_text(f"GEMINI_API_KEY={fake_key}\n", encoding="utf-8")
        assert ensure_gemini_api_key() == fake_key

    def test_ensure_gemini_api_key_env_override(self):
        import os
        orig = os.environ.get("GEMINI_API_KEY")
        try:
            os.environ["GEMINI_API_KEY"] = "MY_CUSTOM_TEST_KEY_12345"
            assert ensure_gemini_api_key() == "MY_CUSTOM_TEST_KEY_12345"
        finally:
            if orig:
                os.environ["GEMINI_API_KEY"] = orig
            else:
                os.environ.pop("GEMINI_API_KEY", None)

# ─── analyze_and_find_clips parsing ──────────────────────────────────────────

class TestAnalyzeAndFindClips:
    def test_parses_json_with_code_fences(self):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = '```json\n[{"start": 10.0, "end": 35.0, "title": "TEST HOOK", "score": 95, "reason": "Great"}]\n```'
        mock_client.models.generate_content.return_value = mock_resp

        clips = analyze_and_find_clips(
            client=mock_client,
            segments=[{"start": 0, "end": 60, "text": "sample"}],
            count=1,
            min_len=20,
            max_len=55,
        )
        assert len(clips) == 1
        assert clips[0]["title"] == "TEST HOOK"
        assert clips[0]["start"] == 10.0

    def test_parses_dict_wrapped_json(self):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = '{"clips": [{"start": 5.0, "end": 28.0, "title": "KEY ADVICE", "score": 90}]}'
        mock_client.models.generate_content.return_value = mock_resp

        clips = analyze_and_find_clips(
            client=mock_client,
            segments=[{"start": 0, "end": 60, "text": "sample"}],
            count=1,
            min_len=20,
            max_len=55,
        )
        assert len(clips) == 1
        assert clips[0]["title"] == "KEY ADVICE"
        assert clips[0]["start"] == 5.0

    def test_retries_on_503_and_succeeds_on_fallback(self):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.text = '[{"start": 0.0, "end": 25.0, "title": "FALLBACK SUCCESS", "score": 88}]'

        # First call fails with 503, second call succeeds
        mock_client.models.generate_content.side_effect = [
            Exception("503 UNAVAILABLE: Model is currently experiencing high demand"),
            mock_resp,
        ]

        clips = analyze_and_find_clips(
            client=mock_client,
            segments=[{"start": 0, "end": 60, "text": "sample"}],
            count=1,
        )
        assert len(clips) == 1
        assert clips[0]["title"] == "FALLBACK SUCCESS"
