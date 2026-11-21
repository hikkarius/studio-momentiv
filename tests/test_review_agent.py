"""
tests/test_review_agent.py — Unit tests for the modernized review_agent.py QA Auditor & Linter.
"""
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from review_agent import AuditIssue, CodeAuditor, run_e2e_smoke_test, run_tests_and_coverage


def test_audit_issue_representation():
    blocker = AuditIssue("BLOCKER", "core.py", 12, "Critical issue")
    warning = AuditIssue("WARNING", "core.py", 15, "Minor issue")
    info = AuditIssue("INFO", "core.py", 20, "Info message")

    assert "🔴 [BLOCKER]" in str(blocker)
    assert "🟡 [WARNING]" in str(warning)
    assert "ℹ️ [INFO]" in str(info)


def test_check_windows_stdout_reconfigure(tmp_path):
    auditor = CodeAuditor(tmp_path)
    entrypoint_without_utf8 = 'if __name__ == "__main__":\n    print("Hello")\n'
    fake_file = tmp_path / "main.py"
    auditor.check_windows_stdout_reconfigure(fake_file, entrypoint_without_utf8)
    assert len(auditor.issues) == 1
    assert "reconfigure" in auditor.issues[0].message

    auditor.issues.clear()
    entrypoint_with_utf8 = 'from utils import setup_console_encoding\nsetup_console_encoding()\nif __name__ == "__main__":\n    pass'
    auditor.check_windows_stdout_reconfigure(fake_file, entrypoint_with_utf8)
    assert len(auditor.issues) == 0


def test_check_subprocess_timeouts(tmp_path):
    auditor = CodeAuditor(tmp_path)
    code_without_timeout = 'import subprocess\nsubprocess.run(["ffmpeg", "-i", "in.mp4"])\n'
    fake_file = tmp_path / "script.py"
    auditor.check_subprocess_timeouts(fake_file, code_without_timeout)
    assert len(auditor.issues) == 1
    assert "timeout=" in auditor.issues[0].message

    auditor.issues.clear()
    code_with_timeout = 'import subprocess\nsubprocess.run(["ffmpeg", "-i", "in.mp4"], timeout=30)\n'
    auditor.check_subprocess_timeouts(fake_file, code_with_timeout)
    assert len(auditor.issues) == 0


def test_check_path_traversal_guards(tmp_path):
    auditor = CodeAuditor(tmp_path)
    server_file = tmp_path / "server.py"

    code_bad = "def stream_media(path): return FileResponse(path)"
    auditor.check_path_traversal_guards(server_file, code_bad)
    assert len(auditor.issues) == 1
    assert "Path Traversal" in auditor.issues[0].message

    auditor.issues.clear()
    code_good = "def stream_media(path):\n target = Path(path).resolve()\n if target.is_relative_to(web_uploads): return"
    auditor.check_path_traversal_guards(server_file, code_good)
    assert len(auditor.issues) == 0


def test_check_db_safety(tmp_path):
    auditor = CodeAuditor(tmp_path)
    db_file = tmp_path / "db.py"

    # Missing foreign keys
    code_bad_fk = "def get_db(): return sqlite3.connect('test.db')"
    auditor.check_db_safety(db_file, code_bad_fk)
    assert any("foreign_keys = ON" in i.message for i in auditor.issues)

    # SQL Injection via f-string
    auditor.issues.clear()
    code_bad_sql = "PRAGMA foreign_keys = ON;\ncursor.execute(f'SELECT * FROM users WHERE id = {user_id}')"
    auditor.check_db_safety(db_file, code_bad_sql)
    assert any("SQL statement uses f-string formatting" in i.message for i in auditor.issues)

    # Safe parameterized query
    auditor.issues.clear()
    code_good = "PRAGMA foreign_keys = ON;\ncursor.execute('SELECT * FROM users WHERE id = ?', (user_id,))"
    auditor.check_db_safety(db_file, code_good)
    assert len(auditor.issues) == 0


def test_check_concurrency_locks(tmp_path):
    auditor = CodeAuditor(tmp_path)

    # Missing lock in job_runner
    jr_file = tmp_path / "job_runner.py"
    code_jr_bad = "def register(): _subscribers.add(q)"
    auditor.check_concurrency_locks(jr_file, code_jr_bad)
    assert any("_subscribers_lock" in i.message for i in auditor.issues)

    # Good lock in job_runner
    auditor.issues.clear()
    code_jr_good = "with _subscribers_lock:\n    _subscribers.add(q)"
    auditor.check_concurrency_locks(jr_file, code_jr_good)
    assert len(auditor.issues) == 0


def test_check_pydantic_v2_usage(tmp_path):
    auditor = CodeAuditor(tmp_path)
    fake_file = tmp_path / "worker.py"

    code_dict_call = "data = my_config.dict()\n"
    auditor.check_pydantic_v2_usage(fake_file, code_dict_call)
    assert len(auditor.issues) == 1
    assert "Deprecated call to '.dict()'" in auditor.issues[0].message

    auditor.issues.clear()
    code_dump_call = "data = my_config.model_dump()\n"
    auditor.check_pydantic_v2_usage(fake_file, code_dump_call)
    assert len(auditor.issues) == 0


def test_check_ast_bare_except(tmp_path):
    auditor = CodeAuditor(tmp_path)
    fake_file = tmp_path / "worker.py"

    code_bare_except = "try:\n    x = 1\nexcept:\n    pass\n"
    auditor.check_ast_rules(fake_file, code_bare_except)
    assert any("Bare 'except:'" in i.message for i in auditor.issues)

    auditor.issues.clear()
    code_good_except = "try:\n    x = 1\nexcept Exception:\n    pass\n"
    auditor.check_ast_rules(fake_file, code_good_except)
    assert len(auditor.issues) == 0


def test_check_api_timeout_guards(tmp_path):
    auditor = CodeAuditor(tmp_path)
    state_file = tmp_path / "state.py"

    code_bad = "client = genai.Client(api_key=key)"
    auditor.check_api_timeout_guards(state_file, code_bad)
    assert len(auditor.issues) == 1
    assert "timeout" in auditor.issues[0].message

    auditor.issues.clear()
    code_good = 'client = genai.Client(api_key=key, http_options={"timeout": 45000})'
    auditor.check_api_timeout_guards(state_file, code_good)
    assert len(auditor.issues) == 0


def test_check_url_download_security(tmp_path):
    auditor = CodeAuditor(tmp_path)
    utils_file = tmp_path / "utils.py"

    code_bad = "def download_from_url(url): run(['yt-dlp', url])"
    auditor.check_url_download_security(utils_file, code_bad)
    assert len(auditor.issues) == 1
    assert "command injection" in auditor.issues[0].message

    auditor.issues.clear()
    code_good = 'def download_from_url(url): parsed.scheme\n run(["yt-dlp", "--", url])'
    auditor.check_url_download_security(utils_file, code_good)
    assert len(auditor.issues) == 0


def test_check_transcription_safety(tmp_path):
    auditor = CodeAuditor(tmp_path)
    transcription_file = tmp_path / "transcription.py"

    code_bad = "def transcribe_video(video_path): return []"
    auditor.check_transcription_safety(transcription_file, code_bad)
    assert len(auditor.issues) == 2
    assert any("vad_filter" in i.message for i in auditor.issues)
    assert any("initial_prompt" in i.message for i in auditor.issues)

    auditor.issues.clear()
    code_good = "def transcribe_video(video_path, vad_filter=True, initial_prompt=''): return []"
    auditor.check_transcription_safety(transcription_file, code_good)
    assert len(auditor.issues) == 0


def test_check_secret_leak_prevention(tmp_path):
    auditor = CodeAuditor(tmp_path)
    config_file = tmp_path / "config.py"

    code_bad = "GEMINI_KEY = 'AIzaSyD-fakeKeyExample12345678901234567'"
    auditor.check_secret_leak_prevention(config_file, code_bad)
    assert len(auditor.issues) == 1
    assert "hardcoded Google API key" in auditor.issues[0].message

    auditor.issues.clear()
    code_good = "GEMINI_KEY = os.getenv('GEMINI_API_KEY')"
    auditor.check_secret_leak_prevention(config_file, code_good)
    assert len(auditor.issues) == 0


@patch("subprocess.run")
def test_run_tests_and_coverage_extracts_final_total(mock_subproc):
    # Tests that when stdout contains multiple mock TOTAL lines, only the final real summary is parsed
    output_with_multiple_totals = (
        "tests/test_foo.py PASSED\n"
        "mock print output: TOTAL 100 15 40%\n"
        "mock print output: TOTAL 100 15 50%\n"
        "================ coverage ================\n"
        "TOTAL                    2050    300    85%\n"
    )
    mock_subproc.return_value = MagicMock(
        returncode=0,
        stdout=output_with_multiple_totals,
        stderr="",
    )

    passed, msg = run_tests_and_coverage(min_coverage=80)
    assert passed is True
    assert "85%" in msg

    # If the real final total is below threshold
    output_low_cov = (
        "mock print output: TOTAL 100 15 95%\n"
        "================ coverage ================\n"
        "TOTAL                    2050    600    70%\n"
    )
    mock_subproc.return_value = MagicMock(
        returncode=0,
        stdout=output_low_cov,
        stderr="",
    )
    passed, msg = run_tests_and_coverage(min_coverage=80)
    assert passed is False
    assert "70%" in msg
    assert "нижче встановленого порогу" in msg


def test_run_e2e_smoke_test():
    passed, msg = run_e2e_smoke_test()
    assert passed is True
    assert "E2E Smoke Test успішно пройдено" in msg


def test_check_cors_policy(tmp_path):
    auditor = CodeAuditor(tmp_path)
    server_file = tmp_path / "server.py"
    auditor.check_cors_policy(server_file, 'app.add_middleware(CORSMiddleware, allow_origins=["*"])\n')
    assert any("Wildcard CORS" in i.message for i in auditor.issues)

    auditor.issues.clear()
    auditor.check_cors_policy(server_file, 'app.add_middleware(CORSMiddleware, allow_origins=["http://127.0.0.1:8000"])\n')
    assert len(auditor.issues) == 0


def test_check_unsafe_path_startswith(tmp_path):
    auditor = CodeAuditor(tmp_path)
    utils_file = tmp_path / "utils.py"
    auditor.check_unsafe_path_startswith(utils_file, "if not abs_path.startswith(abs_output_dir): raise ValueError()\n")
    assert any("prefix bypass" in i.message for i in auditor.issues)

    auditor.issues.clear()
    auditor.check_unsafe_path_startswith(utils_file, "if not resolved_path.is_relative_to(base): raise ValueError()\n")
    assert len(auditor.issues) == 0


def test_check_db_insert_or_replace_warning(tmp_path):
    auditor = CodeAuditor(tmp_path)
    db_file = tmp_path / "db.py"
    code = "PRAGMA foreign_keys = ON;\nconn.execute('INSERT OR REPLACE INTO projects VALUES (?)', (1,))"
    auditor.check_db_safety(db_file, code)
    assert any("ON DELETE CASCADE" in i.message for i in auditor.issues)


def test_check_frontend_xss(tmp_path):
    js_dir = tmp_path / "frontend" / "js"
    js_dir.mkdir(parents=True)
    bad_js = js_dir / "app.js"
    bad_js.write_text('el.innerHTML = `<button onclick="openProject(\'${p.id}\')">Open</button>`;\n', encoding="utf-8")

    auditor = CodeAuditor(tmp_path)
    auditor.check_frontend_xss()
    assert len(auditor.issues) == 1
    assert "XSS risk" in auditor.issues[0].message

    bad_js.write_text('btn.addEventListener("click", () => openProject(p.id));\n', encoding="utf-8")
    auditor.issues.clear()
    auditor.check_frontend_xss()
    assert len(auditor.issues) == 0
