"""
tests/test_disk_space.py — the studio checks free disk space before it downloads a link or takes a file,
and the home page gets a short disk state for its notice.
"""
import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import utils
from backend import db, queue_runner, server, storage
from backend.server import app

client = TestClient(app)
GB = storage.GB


def test_fmt_gb_uses_ukrainian_decimal_comma():
    assert storage.fmt_gb(int(6.8 * GB)) == "6,8 ГБ"
    assert storage.fmt_gb(300 * 1024 ** 2) == "300 МБ"


def test_ensure_space_needs_the_video_plus_a_reserve(monkeypatch, tmp_path):
    monkeypatch.setattr(storage, "free_bytes", lambda p: 3 * GB)
    storage.ensure_space(tmp_path, int(1.5 * GB))  # 1.5 + 1 reserve fits in 3
    with pytest.raises(storage.NotEnoughSpaceError) as err:
        storage.ensure_space(tmp_path, int(5.8 * GB))
    assert str(err.value) == "Не вистачає місця на диску. Треба близько 6,8 ГБ, а вільно лише 3,0 ГБ."
    # an unknown size or unknown free space never blocks the download
    storage.ensure_space(tmp_path, None)
    monkeypatch.setattr(storage, "free_bytes", lambda p: None)
    storage.ensure_space(tmp_path, 500 * GB)


def test_free_bytes_of_a_folder_that_does_not_exist_yet(tmp_path):
    assert storage.free_bytes(tmp_path / "not" / "yet") > 0


@pytest.mark.parametrize("free, level", [(50 * GB, "ok"), (9 * GB, "low"), (int(1.5 * GB), "full")])
def test_disk_status_levels(monkeypatch, tmp_path, free, level):
    monkeypatch.setattr(storage, "free_bytes", lambda p: free)
    st = storage.disk_status(tmp_path)
    assert st["level"] == level and st["free"] == free
    assert st["video_hours"] == round((free - GB) / storage.BYTES_PER_VIDEO_HOUR, 1)


@pytest.mark.parametrize("text", [
    "Не вистачає місця на диску. Треба близько 2 ГБ",
    "ERROR: unable to write data: [Errno 28] No space left on device",
    "OSError: [WinError 112] There is not enough space on the disk",
])
def test_full_disk_errors_are_recognised(text):
    assert storage.is_no_space_error(text)
    assert not storage.is_no_space_error("ERROR: [youtube] Video unavailable")


def test_estimate_download_size_sums_video_and_audio():
    info = {"duration": 3600, "requested_formats": [{"filesize": 2_000_000_000}, {"filesize_approx": 60_000_000}]}
    assert utils.estimate_download_size(info) == 2_060_000_000
    # no sizes: bitrate (kbit/s) × duration
    assert utils.estimate_download_size({"duration": 100, "tbr": 800}) == 10_000_000
    assert utils.estimate_download_size({"title": "x"}) is None


def test_probe_download_size_asks_yt_dlp_without_downloading():
    info = {"duration": 60, "requested_formats": [{"filesize": 900}, {"filesize": 100}]}
    out = MagicMock(returncode=0, stdout="[youtube] x: Downloading\n" + json.dumps(info) + "\n")
    with patch("utils.subprocess.run", return_value=out) as run:
        assert utils.probe_download_size("https://youtube.com/watch?v=abc") == 1000
    cmd = run.call_args.args[0]
    assert "--skip-download" in cmd and cmd[-2:] == ["--", "https://youtube.com/watch?v=abc"]
    assert run.call_args.kwargs["timeout"]
    with patch("utils.subprocess.run", side_effect=subprocess.TimeoutExpired("yt-dlp", 90)):
        assert utils.probe_download_size("https://youtube.com/watch?v=abc") is None
    with pytest.raises(ValueError):
        utils.probe_download_size("http://127.0.0.1/video")


def _big_video(monkeypatch):
    """A 9 GB video and 2 GB free; the download itself must never start."""
    monkeypatch.setattr(server, "probe_download_size", lambda url, **kw: 9 * GB)
    monkeypatch.setattr(storage, "free_bytes", lambda p: 2 * GB)
    dl = MagicMock(side_effect=AssertionError("must not download"))
    monkeypatch.setattr(server, "download_from_url_with_title", dl)
    return dl


def test_link_that_does_not_fit_is_not_downloaded(monkeypatch):
    dl = _big_video(monkeypatch)
    r = client.post("/api/upload-url", json={"url": "https://youtube.com/watch?v=big"})
    assert r.status_code == 507
    assert r.json()["detail"] == "Не вистачає місця на диску. Треба близько 10,0 ГБ, а вільно лише 2,0 ГБ."
    dl.assert_not_called()
    assert not list(server.UPLOADS_DIR.glob("temp_url_*"))


def test_queue_item_that_does_not_fit_fails_with_a_clear_reason(monkeypatch):
    _big_video(monkeypatch)
    client.post("/api/queue", json={"urls": ["https://youtube.com/watch?v=big"]})
    failed = queue_runner.process_next()
    assert failed["status"] == "failed"
    assert failed["error"].startswith("Не вистачає місця на диску. Треба близько 10,0 ГБ")
    item = client.get("/api/queue").json()["items"][0]
    assert item["no_space"] is True
    # other failures do not ask to free space
    db.update_queue_item(item["id"], error="Не вдалося завантажити відео: Video unavailable")
    assert client.get("/api/queue").json()["items"][0]["no_space"] is False


def test_disk_filling_up_during_download_reads_as_no_space(monkeypatch):
    client.post("/api/queue", json={"urls": ["https://youtube.com/watch?v=x"]})

    def boom(url, progress_cb=None):
        raise subprocess.CalledProcessError(1, ["yt-dlp"], "", "ERROR: unable to write data: [Errno 28] No space left on device\n")

    monkeypatch.setattr(server, "ingest_url", boom)
    failed = queue_runner.process_next()
    assert failed["error"] == "Не вдалося завантажити відео: Не вистачає місця на диску: він заповнився під час завантаження."
    assert queue_runner.public_item(failed)["no_space"] is True


def test_file_that_does_not_fit_is_refused_before_it_is_read(monkeypatch):
    monkeypatch.setattr(storage, "free_bytes", lambda p: GB // 2)
    r = client.post("/api/upload", files={"file": ("big.mp4", b"\0" * 4096, "video/mp4")})
    assert r.status_code == 507
    assert "Не вистачає місця" in r.json()["detail"]
    assert not list(server.UPLOADS_DIR.iterdir())


def test_queue_and_disk_endpoints_report_the_disk(monkeypatch):
    monkeypatch.setattr(storage, "free_bytes", lambda p: 9 * GB)
    disk = client.get("/api/queue").json()["disk"]
    assert disk["level"] == "low" and disk["free_text"] == "9,0 ГБ"
    d = client.get("/api/disk").json()
    assert d["level"] == "low" and d["reserve"] == storage.SPACE_RESERVE_BYTES
