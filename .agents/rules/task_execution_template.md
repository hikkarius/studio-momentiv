# Task Execution & Memory Continuity Protocol (v3.0)

Every task in **AI Video Moments Analyzer Studio** follows the Multi-Agent Pipeline and synchronizes with `.antigravity/`:

1. **[PM] Planning**: Read `.antigravity/ARCHITECTURE.md`, `.antigravity/STATE.md`, `.antigravity/DECISIONS.md`; decompose & register the task in `STATE.md`.
2. **[Architect] Design**: Specify interfaces, data contracts, security boundaries, and concurrency invariants before coding.
3. **[Dev] Implementation**: Write clean, typed, Pydantic v2 code with unit & integration tests isolated via `tests/conftest.py`.
4. **[Auditor] Audit**: Run `.venv\Scripts\python.exe review_agent.py --e2e` (0 blockers, 0 warnings, 100% tests passing, coverage $\ge 80\%$) and verify `web_output/` stays clean.
5. **[Reviewer] Final Approval**: Verify diff against checklist, update `.antigravity/STATE.md` and `.antigravity/DECISIONS.md`, and issue `APPROVED` / `REQUEST CHANGES`.
