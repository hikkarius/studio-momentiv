---
name: code-auditor
description: >-
  Automated QA Auditor, Static Linter, and Critical Code Reviewer for AI Video Moments Studio.
  Evaluates code diffs, architectural integrity, edge cases, Windows compatibility,
  security vulnerabilities, database safety, and test coverage.
---

# Code Auditor & QA Reviewer Skill

Activate this skill when evaluating code quality, performing a pull request review, reviewing architectural decisions, or validating code before a release.

## Review Methodology

When reviewing any changes in AI Video Moments Studio:
1. **Analyze Diff**: Inspect all added, modified, or deleted lines.
2. **Windows Gotchas Checklist**:
   - Is UTF-8 explicitly configured for stdout/stderr or `setup_console_encoding()` called?
   - Can any unicode characters trigger `UnicodeEncodeError` under `cp1251`?
   - Are file paths normalized and safe across Windows drive letters and separators?
3. **Security & Boundary Protection**:
   - Are media and file endpoints guarded against **Path Traversal** using strict `Path.resolve()` and allowed root directory containment?
   - Are URL downloads restricted to `http`/`https` and guarded with `--` separator against CLI injection in `download_from_url`?
   - Are hardcoded API keys (e.g. `AIza...`) completely absent from repository code?
   - Are deleted projects purged completely (database record, output files, and physical unreferenced source videos)?
4. **Database Safety & Concurrency**:
   - Does SQLite connection enable `PRAGMA foreign_keys = ON;` and WAL mode?
   - Are all database statements using parameterized queries (`?`), completely avoiding f-string SQL interpolation?
   - Are concurrent operations (e.g. SSE client subscriber sets, on-demand MP4 clip slicing) guarded with thread locks (`threading.Lock`)?
   - Are heavy file I/O operations inside async routes offloaded to worker threads via `asyncio.to_thread`?
5. **AI & Audio Analysis Resilience**:
   - Does Gemini analysis feature multi-model fallback across Flash Lite variants?
   - For long audio (> 180s), is Chronological Audio Windowing applied to prevent attention drift?
   - Are clip virality scores parsed defensively (`_safe_score`), handling integers, strings like `"90/100"`, and None gracefully?
   - Is transcript Whisper extraction guarded with `vad_filter` and proper language detection?
6. **Code Hygiene & Modern Standards**:
   - Are Pydantic v2 schemas using `.model_dump()` rather than legacy `.dict()`?
   - Are bare `except:` statements strictly avoided?
   - Are subprocess executions guarded with mandatory `timeout`?
7. **Automated Verification**:
   - Run `python review_agent.py` to perform automated static auditing and pytest test suite.
   - Run `python review_agent.py --e2e` for backend smoke tests.
   - Run `pytest tests/ --cov=.` to verify 100% test pass rate and genuine coverage $\ge 80\%$.

## Scoring Format
Deliver feedback in 4 clear categories:
- 🔴 **BLOCKER**: Critical defects, security vulnerabilities, crashes, or unhandled exceptions.
- 🟡 **WARNING**: Code smells, missing type annotations, suboptimal performance, missing tests.
- 🟢 **PASS**: Verified patterns and robust implementations.
- 💡 **RECOMMENDATION**: Long-term architectural advice.
