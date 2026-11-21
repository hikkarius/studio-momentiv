"""«Зібрати звіт про проблему»: the ZIP has the logs and info, and never the Gemini key or the user name."""
import io
import zipfile

from fastapi.testclient import TestClient

from backend import server, support_report


def test_redact_hides_keys_and_windows_user_name(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "my-secret-key-123456789")
    text = (
        "key my-secret-key-123456789 and AIza" + "B" * 35 + " and AQ.Ab" + "c" * 30 +
        "\nGEMINI_API_KEY=whatever\nC:\\Users\\Артур\\Downloads\\video.mp4"
    )
    out = support_report.redact(text)
    assert "my-secret-key" not in out and "AIzaB" not in out and "AQ.Ab" not in out
    assert "GEMINI_API_KEY=[КЛЮЧ ПРИХОВАНО]" in out
    assert "Артур" not in out and "C:\\Users\\<user>\\Downloads" in out


def test_report_endpoint_returns_zip_without_key(monkeypatch, tmp_path):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza" + "Q" * 35)
    log = tmp_path / "studio.log"
    log.write_text("start\nusing key " + "AIza" + "Q" * 35 + "\nERROR something broke\n", encoding="utf-8")
    monkeypatch.setattr(server, "_log_file_path", lambda: log)
    from backend import app_update
    monkeypatch.setattr(app_update, "_fetch_latest", lambda repo: (None, None))

    r = TestClient(server.app).get("/api/support-report")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "studio-report-" in r.headers["content-disposition"]
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    assert "звіт.txt" in names and "shorts_automator.log" in names
    everything = "".join(z.read(n).decode("utf-8") for n in names)
    assert "AIzaQQQQ" not in everything
    assert "ERROR something broke" in everything
    assert "відеокарта" in z.read("звіт.txt").decode("utf-8")


def test_long_log_is_cut_to_its_tail(tmp_path):
    log = tmp_path / "big.log"
    log.write_text("старий рядок\n" * 300000 + "ОСТАННІЙ РЯДОК\n", encoding="utf-8")
    text = support_report._tail(log, limit=1024)
    assert text.startswith("[… показано останні") and text.rstrip().endswith("ОСТАННІЙ РЯДОК")
