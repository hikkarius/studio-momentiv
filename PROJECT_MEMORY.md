# 🧠 PROJECT MEMORY — AI Video Moments Analyzer Studio

> Детальні нотатки (архітектура, стан, ADR) — у директорії `.antigravity/`:
> [`ARCHITECTURE.md`](.antigravity/ARCHITECTURE.md), [`STATE.md`](.antigravity/STATE.md), [`DECISIONS.md`](.antigravity/DECISIONS.md).
> Частина описів там може відставати від коду — джерело правди: код і `README.md`.

## Стислий огляд
- **Концепція**: локальна студія пошуку вірусних моментів (кліпи 15–60 с) без 9:16 рендерингу. FastAPI + SQLite WAL + Vanilla JS SPA.
- **Пайплайн (єдиний)**: відео/YouTube-URL → локальний Faster-Whisper (CUDA float16 / CPU int8, кеш `.transcript_cache/`) → Gemini за транскрипцією (`ai_selector.py`) → `validate_clips` → SQLite + `web_output/{project_id}/metadata.*`. Окремого рушія `gemini_cloud` (аудіо у Gemini) більше немає: значення `engine` ігнорується.
- **Помилки Gemini**: перебір усіх `gemini.candidate_models`, до 3 проходів (паузи 5 с / 20 с), далі `GeminiUnavailableError` → задача `failed` з українським повідомленням; в UI кнопка «Спробувати ще раз». Евристичних/фейкових кліпів немає.
- **GPU**: `utils.register_cuda_dll_dirs()` додає `site-packages/nvidia/*/bin` до пошуку DLL (інакше `cublas64_12.dll not found`).
- **Точки входу**: `run_studio.py` (`http://127.0.0.1:8000`) та CLI `repurpose_agent.py`.
- **Перевірка**: `.venv\Scripts\python.exe -m pytest tests -q` (лог тестів іде в `SHORTS_LOG_FILE` у temp).
