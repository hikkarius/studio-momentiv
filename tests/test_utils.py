"""
tests/test_utils.py — Unit tests for utils.py media, FFmpeg, URL security, deduplication, and GPU probing.
"""
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest
import utils
from utils import (
    GpuCapabilities,
    compute_file_fingerprint,
    download_from_url,
    download_from_url_with_title,
    extract_audio,
    extract_clip_slice,
    find_duplicate_video,
    generate_video_thumbnail,
    get_video_duration,
    probe_gpu_capabilities,
    wait_for_file_ready,
)


@patch("subprocess.run")
def test_get_video_duration(mock_run):
    # Non-existent path returns 0.0 without running ffprobe
    assert get_video_duration("non_existent_file.mp4") == 0.0
    assert get_video_duration(None) == 0.0

    # Successful ffprobe run
    mock_run.return_value = MagicMock(returncode=0, stdout="42.5\n")
    with patch("os.path.exists", return_value=True):
        duration = get_video_duration("dummy.mp4")
        assert duration == 42.5

    # Subprocess error returns 0.0
    mock_run.side_effect = subprocess.CalledProcessError(1, "ffprobe")
    with patch("os.path.exists", return_value=True):
        assert get_video_duration("dummy.mp4") == 0.0


@patch("subprocess.run")
def test_extract_audio(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    res = extract_audio("input.mp4", "output.wav")
    assert res == "output.wav"
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args[0] == "ffmpeg"
    assert "pcm_s16le" in args


def test_extract_clip_slice_missing_source():
    with pytest.raises(FileNotFoundError):
        extract_clip_slice("missing_source.mp4", 0.0, 10.0, "out.mp4")


@patch("subprocess.run")
def test_extract_clip_slice_stream_copy_success(mock_run, tmp_path):
    src = tmp_path / "src.mp4"
    src.write_bytes(b"source_video_bytes")
    out = tmp_path / "slices" / "out.mp4"

    def fake_run(cmd, **kwargs):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"X" * 2048)
        return MagicMock(returncode=0, stderr="")

    mock_run.side_effect = fake_run
    res = extract_clip_slice(str(src), 5.0, 20.0, str(out))
    assert res == str(out)
    assert mock_run.call_count == 1
    assert "-c" in mock_run.call_args[0][0] and "copy" in mock_run.call_args[0][0]


@patch("subprocess.run")
def test_extract_clip_slice_nvenc_then_cpu_fallback(mock_run, tmp_path, monkeypatch):
    from state import app_state

    src = tmp_path / "src.mp4"
    src.write_bytes(b"source_video_bytes")
    out = tmp_path / "slices" / "out_fallback.mp4"

    monkeypatch.setattr(
        app_state,
        "get_gpu_capabilities",
        lambda: GpuCapabilities(has_nvenc=True, nvenc_device_name="Mock RTX"),
    )

    # 1: stream copy fails; 2: NVENC fails; 3: libx264 CPU succeeds
    mock_run.side_effect = [
        MagicMock(returncode=1, stderr="copy failed"),
        MagicMock(returncode=1, stderr="nvenc failed"),
        MagicMock(returncode=0, stderr=""),
    ]

    res = extract_clip_slice(str(src), 10.0, 30.0, str(out))
    assert res == str(out)
    assert mock_run.call_count == 3
    assert "h264_nvenc" in mock_run.call_args_list[1][0][0]
    assert "libx264" in mock_run.call_args_list[2][0][0]


@patch("subprocess.run")
def test_extract_clip_slice_cpu_only_fallback(mock_run, tmp_path, monkeypatch):
    from state import app_state

    src = tmp_path / "src.mp4"
    src.write_bytes(b"source_video_bytes")
    out = tmp_path / "out_cpu.mp4"

    monkeypatch.setattr(
        app_state,
        "get_gpu_capabilities",
        lambda: GpuCapabilities(has_nvenc=False),
    )

    mock_run.side_effect = [
        MagicMock(returncode=1, stderr="copy failed"),
        MagicMock(returncode=0, stderr=""),
    ]
    res = extract_clip_slice(str(src), 0.0, 15.0, str(out))
    assert res == str(out)
    assert "libx264" in mock_run.call_args_list[1][0][0]


@patch("subprocess.run")
def test_generate_video_thumbnail(mock_run, tmp_path):
    assert generate_video_thumbnail("missing.mp4", str(tmp_path / "t.jpg")) is None

    src = tmp_path / "vid.mp4"
    src.write_bytes(b"video")
    thumb = tmp_path / "thumbs" / "thumb.jpg"

    def fake_thumb(cmd, **kwargs):
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"\xff\xd8\xff\xe0JFIF")
        return MagicMock(returncode=0)

    mock_run.side_effect = fake_thumb
    res = generate_video_thumbnail(str(src), str(thumb), at_sec=2.0)
    assert res == str(thumb)

    # On ffmpeg error returns None gracefully
    mock_run.side_effect = subprocess.CalledProcessError(1, "ffmpeg")
    assert generate_video_thumbnail(str(src), str(tmp_path / "fail.jpg")) is None


@patch("subprocess.run")
def test_download_from_url_security_and_success(mock_subproc, tmp_path):
    # Mock yt-dlp returning valid path inside output_dir
    target_file = tmp_path / "video123.mp4"
    target_file.write_bytes(b"dummy video data")

    mock_subproc.return_value = MagicMock(
        returncode=0,
        stdout=f"{target_file}\n",
    )

    downloaded = download_from_url("https://youtube.com/watch?v=123", output_dir=str(tmp_path))
    assert downloaded == os.path.normpath(str(target_file))

    # The real video title is printed by yt-dlp on the line before the file path
    mock_subproc.return_value = MagicMock(returncode=0, stdout=f"Чи варто грати у WoW?\n{target_file}\n")
    path, title = download_from_url_with_title("https://youtube.com/watch?v=123", output_dir=str(tmp_path))
    assert path == os.path.normpath(str(target_file))
    assert title == "Чи варто грати у WoW?"

    # Path traversal attempt should raise ValueError
    mock_subproc.return_value = MagicMock(
        returncode=0,
        stdout="C:\\Windows\\System32\\calc.exe\n" if sys.platform == "win32" else "/etc/passwd\n",
    )
    with pytest.raises(ValueError, match="resolved outside output directory"):
        download_from_url("https://youtube.com/watch?v=123", output_dir=str(tmp_path))

    # SEC-02: Prefix-bypass traversal (sibling dir sharing prefix) must also raise ValueError
    base_dir = tmp_path / "uploads"
    sibling_dir = tmp_path / "uploads_evil"
    base_dir.mkdir()
    sibling_dir.mkdir()
    sibling_file = sibling_dir / "pwned.mp4"
    sibling_file.write_bytes(b"evil")
    mock_subproc.return_value = MagicMock(returncode=0, stdout=f"{sibling_file}\n")
    with pytest.raises(ValueError, match="resolved outside output directory"):
        download_from_url("https://youtube.com/watch?v=123", output_dir=str(base_dir))


def test_download_from_url_scheme_and_ssrf_validation():
    with pytest.raises(ValueError, match="URL cannot be empty"):
        download_from_url("")
    with pytest.raises(ValueError, match="Неприпустима схема URL"):
        download_from_url("file:///etc/passwd")
    with pytest.raises(ValueError, match="Неприпустима схема URL"):
        download_from_url("ftp://example.com/video.mp4")
    with pytest.raises(ValueError, match="заборонено"):
        download_from_url("http://localhost:8000/api/projects")
    with pytest.raises(ValueError, match="заборонено"):
        download_from_url("http://127.0.0.1:8000/secret")
    with pytest.raises(ValueError, match="заборонено"):
        download_from_url("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(ValueError, match="заборонено"):
        download_from_url("http://192.168.1.1/admin")


@patch("subprocess.run")
def test_probe_gpu_capabilities_ctranslate2_cuda(mock_run):
    def mock_subproc(cmd, **kwargs):
        if "nvidia-smi" in cmd[0]:
            return MagicMock(returncode=0, stdout="NVIDIA GeForce RTX 4050\n")
        return MagicMock(returncode=0)

    mock_run.side_effect = mock_subproc
    mock_ct2 = MagicMock()
    mock_ct2.get_cuda_device_count.return_value = 1

    with patch.dict("sys.modules", {"ctranslate2": mock_ct2, "torch": None}):
        caps = probe_gpu_capabilities()
        assert caps.has_nvenc is True
        assert caps.has_cuda is True
        assert "RTX 4050" in caps.cuda_device_name

    # No GPU / FileNotFoundError
    mock_run.side_effect = FileNotFoundError()
    mock_ct2.get_cuda_device_count.return_value = 0
    with patch.dict("sys.modules", {"ctranslate2": mock_ct2, "torch": None}):
        caps_none = probe_gpu_capabilities()
        assert caps_none.has_nvenc is False
        assert caps_none.has_cuda is False


def test_wait_for_file_ready(tmp_path):
    assert wait_for_file_ready(None) is False
    assert wait_for_file_ready(str(tmp_path / "absent.mp4")) is False

    test_file = tmp_path / "ready.mp4"
    test_file.write_bytes(b"ready media content")
    assert wait_for_file_ready(str(test_file)) is True


def test_find_duplicate_video(tmp_path):
    assert compute_file_fingerprint("") == (0, "")
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    assert compute_file_fingerprint(str(empty)) == (0, "")

    v1 = tmp_path / "video1.mp4"
    v1.write_bytes(b"A" * 100000)

    v2 = tmp_path / "video2.mp4"
    v2.write_bytes(b"A" * 100000)

    v3 = tmp_path / "video3.mp4"
    v3.write_bytes(b"B" * 100000)

    v4 = tmp_path / "video4.mp4"
    v4.write_bytes(b"A" * 50000)

    assert compute_file_fingerprint(str(v1)) == compute_file_fingerprint(str(v2))
    assert compute_file_fingerprint(str(v1)) != compute_file_fingerprint(str(v3))
    assert compute_file_fingerprint(str(v1)) != compute_file_fingerprint(str(v4))

    dup = find_duplicate_video(str(v2), search_dir=str(tmp_path))
    assert dup is not None
    assert os.path.abspath(dup) == os.path.abspath(str(v1))
    assert find_duplicate_video(str(v3), search_dir=str(tmp_path)) is None
    assert find_duplicate_video("nonexistent.mp4", search_dir=str(tmp_path)) is None


def test_register_cuda_dll_dirs_finds_nvidia_bins_and_is_idempotent(tmp_path, monkeypatch):
    site_pkgs = tmp_path / "site-packages"
    (site_pkgs / "nvidia" / "cublas" / "bin").mkdir(parents=True)
    (site_pkgs / "nvidia" / "cudnn" / "bin").mkdir(parents=True)
    (site_pkgs / "nvidia" / "no_bin_pkg").mkdir(parents=True)

    import site
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(site, "getsitepackages", lambda: [str(site_pkgs)])
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "no_such_prefix"))
    monkeypatch.setattr(utils, "_cuda_dll_dirs_registered", set())
    monkeypatch.setattr(utils, "_cuda_dll_handles", [])
    added = []
    monkeypatch.setattr(os, "add_dll_directory", lambda d: added.append(d) or object(), raising=False)
    monkeypatch.setenv("PATH", "ORIGINAL")

    found = utils.register_cuda_dll_dirs()
    assert len(found) == 2
    assert all(f.endswith("bin") for f in found)
    assert len(added) == 2
    assert os.environ["PATH"].endswith("ORIGINAL")
    assert all(f in os.environ["PATH"] for f in found)

    # Second call must not duplicate PATH entries or DLL handles
    path_before = os.environ["PATH"]
    utils.register_cuda_dll_dirs()
    assert os.environ["PATH"] == path_before
    assert len(added) == 2


def test_register_cuda_dll_dirs_never_raises(monkeypatch):
    import site
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(site, "getsitepackages", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert isinstance(utils.register_cuda_dll_dirs(), list)


def test_register_cuda_dll_dirs_noop_on_non_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert utils.register_cuda_dll_dirs() == []
