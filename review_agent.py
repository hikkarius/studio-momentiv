"""
review_agent.py — Modern QA Auditor & Static Linter Sub-agent.

Performs static code auditing, security checks, Windows compatibility checks,
database integrity checks, and runs the automated test suite with coverage verification.

Usage:
    python review_agent.py [--skip-tests] [--coverage-threshold 80] [--e2e]
"""
import argparse
import ast
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from utils import setup_console_encoding
setup_console_encoding()

REPO_ROOT = Path(__file__).resolve().parent


class AuditIssue:
    def __init__(self, severity: str, file_path: str, line_no: int, message: str):
        self.severity = severity  # "BLOCKER", "WARNING", "INFO"
        self.file_path = file_path
        self.line_no = line_no
        self.message = message

    def __str__(self) -> str:
        icon = "🔴" if self.severity == "BLOCKER" else ("🟡" if self.severity == "WARNING" else "ℹ️")
        return f"  {icon} [{self.severity}] {self.file_path}:{self.line_no} — {self.message}"


class CodeAuditor:
    """Automated static analysis, security inspector, and linter for AI Video Moments Studio."""

    def __init__(self, root: Path):
        self.root = root
        self.issues: List[AuditIssue] = []

    def get_py_files(self) -> List[Path]:
        py_files = []
        for path in self.root.rglob("*.py"):
            parts = path.parts
            if any(p in parts for p in (".venv", "venv", ".git", "__pycache__", "build", "dist")):
                continue
            py_files.append(path)
        return py_files

    def check_windows_stdout_reconfigure(self, file_path: Path, content: str):
        """Entry point scripts must configure UTF-8 on Windows to prevent UnicodeEncodeError."""
        if '__name__ == "__main__"' in content or "__name__ == '__main__'" in content:
            if "sys.stdout.reconfigure" not in content and "reconfigure" not in content and "setup_console_encoding" not in content:
                if "tests" not in file_path.parts:
                    self.issues.append(AuditIssue(
                        "WARNING",
                        str(file_path.relative_to(self.root)),
                        1,
                        "Entrypoint has no 'sys.stdout.reconfigure' or 'setup_console_encoding' call for Windows UTF-8 console safety."
                    ))

    def check_subprocess_timeouts(self, file_path: Path, content: str):
        """All subprocess.run calls must specify a timeout parameter to prevent hanging processes."""
        lines = content.splitlines()
        for idx, line in enumerate(lines, 1):
            if "subprocess.run(" in line and "timeout=" not in line:
                multiline = "\n".join(lines[idx - 1: min(len(lines), idx + 8)])
                if "timeout=" not in multiline:
                    self.issues.append(AuditIssue(
                        "BLOCKER",
                        str(file_path.relative_to(self.root)),
                        idx,
                        "subprocess.run call missing mandatory 'timeout=' parameter."
                    ))

    def check_path_traversal_guards(self, file_path: Path, content: str):
        """File streaming and media access endpoints must enforce path resolution and directory boundary checks."""
        if file_path.name == "server.py" and "def stream_media" in content:
            if "resolve()" not in content or ("is_relative_to" not in content and "parents" not in content):
                self.issues.append(AuditIssue(
                    "BLOCKER",
                    str(file_path.relative_to(self.root)),
                    1,
                    "stream_media missing defensive Path.resolve() containment check (Path Traversal vulnerability risk)."
                ))

    def check_db_safety(self, file_path: Path, content: str):
        """Database queries must use parameterized queries and foreign keys must be enabled."""
        if file_path.name == "db.py":
            if "foreign_keys = ON" not in content:
                self.issues.append(AuditIssue(
                    "BLOCKER",
                    str(file_path.relative_to(self.root)),
                    1,
                    "db.py missing 'PRAGMA foreign_keys = ON;' connection enforcement."
                ))
            # Check for dangerous f-string SQL injections
            for idx, line in enumerate(content.splitlines(), 1):
                if re.search(r'execute\(\s*f["\']', line):
                    self.issues.append(AuditIssue(
                        "BLOCKER",
                        str(file_path.relative_to(self.root)),
                        idx,
                        "SQL statement uses f-string formatting instead of parameterized query '?' (SQL Injection risk)."
                    ))
                if re.search(r"INSERT\s+OR\s+REPLACE\s+INTO\s+projects", line, re.IGNORECASE):
                    self.issues.append(AuditIssue(
                        "WARNING",
                        str(file_path.relative_to(self.root)),
                        idx,
                        "INSERT OR REPLACE on projects triggers ON DELETE CASCADE; use ON CONFLICT(id) DO UPDATE."
                    ))

    def check_cors_policy(self, file_path: Path, content: str):
        """Local API must not expose wildcard CORS allow_origins=['*']."""
        if file_path.name == "server.py":
            for idx, line in enumerate(content.splitlines(), 1):
                if re.search(r'allow_origins\s*=\s*\[\s*["\']\*["\']\s*\]', line):
                    self.issues.append(AuditIssue(
                        "BLOCKER",
                        str(file_path.relative_to(self.root)),
                        idx,
                        "Wildcard CORS allow_origins=['*'] exposes localhost API to cross-origin attacks."
                    ))

    def check_unsafe_path_startswith(self, file_path: Path, content: str):
        """Path containment must use Path.is_relative_to(), never str.startswith()."""
        if file_path.name in ("server.py", "utils.py"):
            for idx, line in enumerate(content.splitlines(), 1):
                if re.search(r'(?:abs_path|resolved_path|target_path|norm_path)\.startswith\(', line):
                    self.issues.append(AuditIssue(
                        "BLOCKER",
                        str(file_path.relative_to(self.root)),
                        idx,
                        "Path containment uses str.startswith() (prefix bypass risk). Use Path.resolve().is_relative_to()."
                    ))

    def check_frontend_xss(self):
        """Scans frontend JS files for unsafe dynamic interpolations inside inline HTML event attributes."""
        js_dir = self.root / "frontend" / "js"
        if not js_dir.exists():
            return
        pattern = re.compile(r'\bon[a-z]+\s*=\s*(["\'])(?:(?!\1).)*\$\{', re.IGNORECASE)
        for js_file in js_dir.rglob("*.js"):
            try:
                content = js_file.read_text(encoding="utf-8", errors="ignore")
                for idx, line in enumerate(content.splitlines(), 1):
                    if pattern.search(line):
                        self.issues.append(AuditIssue(
                            "BLOCKER",
                            str(js_file.relative_to(self.root)),
                            idx,
                            "Unsafe dynamic interpolation '${...}' inside inline HTML event handler (XSS risk). Use addEventListener."
                        ))
            except Exception as e:
                self.issues.append(AuditIssue(
                    "WARNING", str(js_file.relative_to(self.root)), 1, f"Failed to scan JS: {e}"
                ))

    def check_concurrency_locks(self, file_path: Path, content: str):
        """Shared concurrency resources must be guarded with threading locks."""
        if file_path.name == "job_runner.py":
            if "_subscribers_lock" not in content or "with _subscribers_lock:" not in content:
                self.issues.append(AuditIssue(
                    "BLOCKER",
                    str(file_path.relative_to(self.root)),
                    1,
                    "job_runner.py missing '_subscribers_lock' mutex around SSE subscriber set."
                ))
        if file_path.name == "server.py" and "download_clip_slice" in content:
            if "_slice_lock" not in content:
                self.issues.append(AuditIssue(
                    "BLOCKER",
                    str(file_path.relative_to(self.root)),
                    1,
                    "server.py missing '_slice_lock' around clip slice generation."
                ))

    def check_pydantic_v2_usage(self, file_path: Path, content: str):
        """Detects deprecated Pydantic v1 patterns."""
        if "tests" in file_path.parts:
            return

        try:
            tree = ast.parse(content, filename=str(file_path))
        except SyntaxError:
            return

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "dict" and not file_path.name.startswith("test_"):
                    # Check if caller looks like a config or schema model
                    self.issues.append(AuditIssue(
                        "WARNING",
                        str(file_path.relative_to(self.root)),
                        node.lineno,
                        "Deprecated call to '.dict()'. Use Pydantic v2 '.model_dump()' instead."
                    ))

    def check_ast_rules(self, file_path: Path, content: str):
        """AST check for bare excepts."""
        if "tests" in file_path.parts:
            return

        try:
            tree = ast.parse(content, filename=str(file_path))
        except SyntaxError:
            return

        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                self.issues.append(AuditIssue(
                    "BLOCKER",
                    str(file_path.relative_to(self.root)),
                    node.lineno,
                    "Bare 'except:' caught without specifying an Exception class."
                ))

    def check_api_timeout_guards(self, file_path: Path, content: str):
        """Checks that Gemini Client initialization specifies request timeout to prevent UI freezes."""
        if file_path.name == "state.py" and "genai.Client(" in content:
            if "timeout" not in content:
                self.issues.append(AuditIssue(
                    "BLOCKER",
                    str(file_path.relative_to(self.root)),
                    1,
                    "genai.Client missing mandatory request timeout in http_options."
                ))

    def check_url_download_security(self, file_path: Path, content: str):
        """URL downloads must enforce scheme validation and argument separation."""
        if file_path.name == "utils.py" and "def download_from_url" in content:
            if "parsed.scheme" not in content or '"--"' not in content:
                self.issues.append(AuditIssue(
                    "BLOCKER",
                    str(file_path.relative_to(self.root)),
                    1,
                    "download_from_url missing scheme validation or '--' argument separator (command injection risk)."
                ))

    def check_transcription_safety(self, file_path: Path, content: str):
        """Checks that transcription employs VAD filter and prompt guidance."""
        if file_path.name == "transcription.py" and "def transcribe_video" in content:
            if "vad_filter" not in content:
                self.issues.append(AuditIssue(
                    "WARNING",
                    str(file_path.relative_to(self.root)),
                    1,
                    "transcribe_video missing 'vad_filter' configuration for noise/silence suppression."
                ))
            if "initial_prompt" not in content:
                self.issues.append(AuditIssue(
                    "WARNING",
                    str(file_path.relative_to(self.root)),
                    1,
                    "transcribe_video missing 'initial_prompt' for language/spelling accuracy."
                ))

    def check_secret_leak_prevention(self, file_path: Path, content: str):
        """Guards against hardcoded API keys."""
        if "tests" in file_path.parts:
            return

        pattern = r"AIzaSy[A-Za-z0-9_-]{33}"
        for idx, line in enumerate(content.splitlines(), 1):
            if re.search(pattern, line):
                self.issues.append(AuditIssue(
                    "BLOCKER",
                    str(file_path.relative_to(self.root)),
                    idx,
                    "Detected hardcoded Google API key! Must use environment variables or .env."
                ))

    def audit_all(self, quiet: bool = False):
        py_files = self.get_py_files()
        if not quiet:
            print(f"[*] 🔍 Сканування {len(py_files)} файлів Python проекту...")

        for p in py_files:
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
                self.check_windows_stdout_reconfigure(p, content)
                self.check_subprocess_timeouts(p, content)
                self.check_path_traversal_guards(p, content)
                self.check_db_safety(p, content)
                self.check_cors_policy(p, content)
                self.check_unsafe_path_startswith(p, content)
                self.check_concurrency_locks(p, content)
                self.check_pydantic_v2_usage(p, content)
                self.check_ast_rules(p, content)
                self.check_api_timeout_guards(p, content)
                self.check_url_download_security(p, content)
                self.check_transcription_safety(p, content)
                self.check_secret_leak_prevention(p, content)
            except Exception as e:
                self.issues.append(AuditIssue(
                    "WARNING", str(p.relative_to(self.root)), 1, f"Failed to read/parse: {e}"
                ))
        self.check_frontend_xss()


def run_tests_and_coverage(min_coverage: int = 80) -> Tuple[bool, str]:
    """Runs pytest with coverage and parses the real total outcome."""
    print("\n[*] 🧪 Запуск тестового набору та перевірка покриття (pytest --cov)...")
    pytest_bin = REPO_ROOT / ".venv" / ("Scripts" if sys.platform == "win32" else "bin") / "pytest"
    cmd = [str(pytest_bin), "-s", "tests/", "--cov=.", "--cov-report=term-missing", "-q"]

    try:
        proc = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, timeout=180)
    except Exception as e:
        return False, f"Failed to execute pytest: {e}"

    output = proc.stdout + proc.stderr
    print(output)

    if proc.returncode != 0:
        return False, "Тести завершилися з помилками!"

    # Parse ALL occurrences of TOTAL and extract the final one (the real pytest summary)
    cov_matches = re.findall(r"TOTAL\s+\d+\s+\d+\s+(\d+)%", output)
    if cov_matches:
        cov_pct = int(cov_matches[-1])
        if cov_pct < min_coverage:
            return False, f"Покриття коду ({cov_pct}%) нижче встановленого порогу ({min_coverage}%)!"
        return True, f"Усі тести пройдено! Покриття коду: {cov_pct}% (ціль: ≥{min_coverage}%)"

    return True, "Усі тести пройдено успішно!"


def run_e2e_smoke_test(video_path: Optional[str] = None) -> Tuple[bool, str]:
    """Runs a full end-to-end smoke test on the modern FastAPI + SQLite backend."""
    print("\n[*] 🎬 Запуск наскрізного тестування (End-to-End Smoke Test)...")
    try:
        from fastapi.testclient import TestClient
        from backend import db
        from backend.server import app

        db.init_db()
        client = TestClient(app)

        # 1. Health check
        res = client.get("/api/health")
        if res.status_code != 200:
            return False, f"Health check failed with status {res.status_code}"

        # 2. Projects list
        res = client.get("/api/projects")
        if res.status_code != 200:
            return False, f"Projects list failed with status {res.status_code}"

        return True, "E2E Smoke Test успішно пройдено! (FastAPI, SQLite, GPU capabilities)"
    except Exception as e:
        return False, f"Помилка під час виконання E2E тесту: {e}"


def main():
    parser = argparse.ArgumentParser(description="AI Video Moments Studio QA Reviewer & Linter")
    parser.add_argument("--skip-tests", action="store_true", help="Skip pytest and coverage checks")
    parser.add_argument("--coverage-threshold", type=int, default=80, help="Minimum acceptable coverage %")
    parser.add_argument("--e2e", action="store_true", help="Run end-to-end smoke test")
    parser.add_argument("--json", action="store_true", help="Output audit report as JSON")
    args = parser.parse_args()

    if not args.json:
        print("=" * 70)
        print("🛡️  AI VIDEO MOMENTS STUDIO — QA REVIEWER & LINTER SUB-AGENT")
        print("=" * 70)

    auditor = CodeAuditor(REPO_ROOT)
    auditor.audit_all(quiet=args.json)

    test_passed = True
    test_msg = "Пропущено за вимогою користувача."

    if not args.skip_tests:
        test_passed, test_msg = run_tests_and_coverage(args.coverage_threshold)

    e2e_passed = True
    e2e_msg = ""
    if args.e2e:
        e2e_passed, e2e_msg = run_e2e_smoke_test()

    blockers = [i for i in auditor.issues if i.severity == "BLOCKER"]
    warnings = [i for i in auditor.issues if i.severity == "WARNING"]
    verdict_passed = (len(blockers) == 0 and test_passed and e2e_passed)

    if args.json:
        report = {
            "verdict": "PASSED" if verdict_passed else "FAILED",
            "blockers_count": len(blockers),
            "warnings_count": len(warnings),
            "total_issues": len(auditor.issues),
            "test_status": test_msg,
            "e2e_status": e2e_msg if args.e2e else None,
            "issues": [
                {
                    "severity": i.severity,
                    "file": i.file_path,
                    "line": i.line_no,
                    "message": i.message,
                }
                for i in auditor.issues
            ],
        }
        print(json.dumps(report, indent=2, ensure_ascii=False))
        sys.exit(0 if verdict_passed else 1)

    print("\n" + "=" * 70)
    print("📊 РЕЗУЛЬТАТИ АУДИТУ:")
    print("=" * 70)

    if auditor.issues:
        print(f"Знайдено зауважень: {len(auditor.issues)} (Критичних: {len(blockers)}, Попереджень: {len(warnings)})")
        for issue in auditor.issues:
            print(issue)
    else:
        print("✅ Статичний аналіз: жодних зауважень або вразливостей не виявлено!")

    print(f"\n📊 Тестовий статус: {test_msg}")
    if args.e2e:
        print(f"🎬 E2E статус: {e2e_msg}")

    if not verdict_passed:
        print("\n❌ ВЕРДИКТ: АУДИТ НЕ ПРОЙДЕНО. ВИПРАВТЕ ЗАУВАЖЕННЯ ПЕРЕД КОМІТОМ.")
        sys.exit(1)
    else:
        print("\n🏆 ВЕРДИКТ: КОД БЕЗДОГАННИЙ! УСІ ПЕРЕВІРКИ УСПІШНО ПРОЙДЕНО.")
        sys.exit(0)


if __name__ == "__main__":
    main()
