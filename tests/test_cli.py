"""
tests/test_cli.py — Unit tests for repurpose_agent.py CLI entrypoint.
"""
import os
from unittest.mock import MagicMock, patch
import pytest

from repurpose_agent import run_agent, main


def test_run_agent_file_not_found():
    with pytest.raises(FileNotFoundError):
        run_agent("non_existent_video_xyz.mp4")


def test_run_agent_success(tmp_path):
    video_file = tmp_path / "test_input.mp4"
    video_file.write_bytes(b"dummy video content")

    mock_segments = [{"start": 10.0, "end": 40.0, "text": "Цікава думка"}]
    mock_clips = [{
        "start": 10.0,
        "end": 40.0,
        "title": "ХАЙЛАЙТ",
        "score": 95,
        "hook": "Цікава думка...",
        "reason": "Емоційний початок",
        "caption": "Дивіться до кінця",
        "hashtags": ["#shorts", "#інсайти"],
    }]

    out_dir = str(tmp_path / "out_moments")

    with patch("repurpose_agent.get_video_duration", return_value=60.0), \
         patch("repurpose_agent.transcribe_video", return_value=(mock_segments, [], "uk", True)), \
         patch("repurpose_agent.analyze_and_find_clips", return_value=mock_clips), \
         patch("repurpose_agent.validate_clips", return_value=mock_clips):

        clips = run_agent(str(video_file), clips_count=1, output_dir=out_dir)
        assert len(clips) == 1
        assert clips[0]["title"] == "ХАЙЛАЙТ"
        assert os.path.exists(os.path.join(out_dir, "metadata.txt"))
        assert os.path.exists(os.path.join(out_dir, "metadata.json"))


def test_cli_main_entrypoint(tmp_path, monkeypatch):
    video_file = tmp_path / "cli_vid.mp4"
    video_file.write_bytes(b"dummy")
    out_dir = str(tmp_path / "cli_out")

    mock_clips = [{"start": 1.0, "end": 25.0, "title": "CLI CLIP", "hook": "Start hook"}]
    monkeypatch.setattr("sys.argv", ["repurpose_agent.py", str(video_file), "-c", "2", "-o", out_dir, "-f", "advice"])
    with patch("repurpose_agent.run_agent", return_value=mock_clips) as mock_run:
        main()
        mock_run.assert_called_once()


def test_run_studio_launcher(monkeypatch):
    import run_studio
    from utils import GpuCapabilities

    opened_urls = []
    monkeypatch.setattr("time.sleep", lambda _: None)
    monkeypatch.setattr("webbrowser.open", lambda url: opened_urls.append(url))
    run_studio.open_browser_delayed("http://127.0.0.1:8000", delay=0.0)
    assert opened_urls == ["http://127.0.0.1:8000"]

    monkeypatch.setattr(
        run_studio.app_state,
        "get_gpu_capabilities",
        lambda: GpuCapabilities(has_nvenc=True, nvenc_device_name="Mock RTX"),
    )
    # A real studio may already listen on :8000 on the laptop; the launcher must see a free port here
    class _FreePort:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def settimeout(self, t):
            pass

        def connect_ex(self, addr):
            return 111  # connection refused

    monkeypatch.setattr("socket.socket", _FreePort)
    uvicorn_calls = []
    mock_thread = MagicMock()
    monkeypatch.setattr("threading.Thread", lambda *args, **kwargs: mock_thread)
    with patch("uvicorn.run", side_effect=lambda *args, **kwargs: uvicorn_calls.append((args, kwargs))):
        run_studio.main()
    assert len(uvicorn_calls) == 1
    assert uvicorn_calls[0][0][0] == "backend.server:app"
    mock_thread.start.assert_called_once()
