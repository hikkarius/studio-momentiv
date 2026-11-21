# QA Guardian & Critical Code Reviewer Rule (v3.0)

Whenever code changes, refactorings, or new features are proposed or implemented in **AI Video Moments Analyzer Studio**, verify against this checklist:

## Mandatory Verification Protocol

### 1. Windows Compatibility & Encoding
- All CLI/server entry points call `setup_console_encoding()` (`sys.stdout.reconfigure(encoding="utf-8", errors="replace")`).
- All paths use `pathlib.Path` or `os.path.join`.
- All `subprocess.run()` calls include explicit `timeout=`.

### 2. Security & Boundary Protection
- CORS restricted to local origins (`127.0.0.1:8000`, `localhost:8000`, or explicit `CORS_ALLOWED_ORIGINS`).
- Path containment uses `Path.resolve().is_relative_to()`, never `str.startswith()`.
- `/api/upload` enforces video extension whitelist and size cap; `/api/media` enforces media extension whitelist + `nosniff` + `sandbox`.
- No hardcoded API keys (`AIza...`) and no inline `onclick` JS interpolations in `frontend/js/`.

### 3. Resource Management, Concurrency & Database Safety
- Shared singletons (`WhisperModel`, `genai.Client`, `GpuCapabilities`) live in `AppState` (`state.py`) with thread locks.
- SQLite uses `PRAGMA foreign_keys = ON`, WAL mode, parameterized queries (`?`), explicit rollback, and `ON CONFLICT DO UPDATE`.
- Log files use `RotatingFileHandler` (`logger.py`).

### 4. Test Isolation & Coverage Guard
- Tests must isolate `DB_PATH`, `UPLOADS_DIR`, and `OUTPUT_DIR` via `tests/conftest.py` (`_isolated_studio/`) and leave `web_output/` spotless.
- Execute `.venv\Scripts\python.exe review_agent.py --e2e`: must pass 100% of tests with coverage $\ge 80\%$ and 0 blockers.
