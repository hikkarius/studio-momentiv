# Студія моментів — нотатки для Claude

Локальна веб-студія: довге відео (YouTube-посилання або файл, 10 хв – 3 год) → Faster-Whisper транскрибує
локально (GPU NVIDIA або CPU) → Gemini за транскрипцією обирає вірусні моменти 15–60 с → у браузері
монтажний стіл, скачування чистих 16:9 кліпів і вертикальних 9:16 з субтитрами.
Користувачі: автор (Артур) і його друзі на Windows, кожен зі своїм безкоштовним ключем Gemini.

Мова: інтерфейс, повідомлення користувачу, README і коментарі для людей — українською.
Код, імена й повідомлення комітів — англійською.

## Карта коду

- `run_studio.py` — точка входу, http://127.0.0.1:8000. `repurpose_agent.py` — CLI.
- `backend/server.py` — FastAPI, усі `/api/*` маршрути. `backend/db.py` — SQLite (WAL, параметризовані запити).
- `backend/job_runner.py` (аналіз одного відео), `backend/queue_runner.py` (черга, по одному за раз),
  `backend/storage.py` (місце на диску, очищення), `backend/backup.py` (щоденна копія бази в `backups/daily`,
  «Відновити»), `backend/keep_awake.py` (ноутбук не засинає, поки працює черга), `backend/app_update.py` (версія, кнопка «Оновити»,
  перевірка ключа), `backend/support_report.py` («Зібрати звіт про проблему»).
- `transcription.py` (Whisper + кеш `.transcript_cache/`), `ai_selector.py` (Gemini: перебір
  `gemini.candidate_models`, до 3 проходів, потім `GeminiUnavailableError`; ніколи не вигадувати кліпи),
  `clip_refiner.py` (межі кліпів), `vertical_render.py` (9:16, кадрування за обличчям YuNet, субтитри ASS),
  `subtitles_generator.py` (SRT).
- `config.yaml` — значення за замовчуванням; `user_settings.json` (екран «Налаштування») поверх нього;
  ключ у `.env` (`GEMINI_API_KEY`). `config_loader.py`, `config_schema.py`.
- `frontend/` — vanilla JS без збірки: `app.js` (головна), `editor.js` (монтажний стіл), `vertical.js`,
  `queue.js`, `settings.js`, `welcome.js` (перший запуск, ключ), `theme.js` (ключ localStorage `studio-theme`).
  Стилі: `frontend/css/tokens.css` — єдина дизайн-система (кольори обох тем, шкала шрифтів `--fs-*`, заокруглення
  `--r-*`); інші CSS беруть значення лише звідти. `scripts/design_shots.py` — знімки екранів до/після, `scripts/css_stats.py` — підрахунок.
- `installer/install.ps1`, `installer/update.ps1` + `Встановити.bat`, `Запустити.bat`, `Оновити.bat` —
  інсталятор для друзів (uv + власний Python у `tools/`, ffmpeg у `tools/ffmpeg`, `.venv`).
- `version.json` + `.github/workflows/release.yml` — реліз (див. скіл `release-for-friends`).
- Gemini API: перед зміною коду з Gemini (`ai_selector.py`, `state.py`, `backend/app_update.py`) читати скіл
  `gemini-api-dev` (`.claude/skills/gemini-api-dev`, копія від Google) і шукати актуальну документацію через
  MCP `gemini-docs` (`.mcp.json`). Моделі й параметри змінюються швидше, ніж знання моделі.
  Студія поки на `generate_content`; перехід на Interactions API — лише за окремим рішенням автора.
- `.antigravity/`, `PROJECT_MEMORY.md`, `.agents/` — старі нотатки з Google Antigravity; можуть відставати
  від коду. Чеклист рев'ю звідти корисний: `.agents/rules/qa_guardian.md`.

## Команди

Windows (ноутбук автора, `C:\studio_momentiv`):
- запуск: `.venv\Scripts\python.exe run_studio.py` (або ярлик «Студія моментів»)
- тести: `.venv\Scripts\python.exe -m pytest tests -q`

Хмарна сесія Linux:
- `uv venv -p 3.12 .venv && uv pip install -p .venv -r requirements-base.txt httpx pytest pytest-cov playwright`
- `.venv/bin/python -m pytest tests -q -o addopts=""` (≈280 тестів, ~1,5 хв; `tests/e2e` — браузерні,
  Playwright + Chromium з `/opt/pw-browsers/chromium`)
- запустити й подивитися: скіл `run-studio`.

## Правила

- Чистий 16:9 кліп завжди має лишатися доступним для скачування (автор монтує в CapCut).
- Кадрування для вертикалі: лише «Авто (за обличчям)» і «Весь кадр».
- Нічого не ламати в даних друзів при оновленні: `update.ps1` зберігає `.env`, `user_settings.json`,
  `studio_data.db*`, `web_output/`, `web_uploads/`, `.venv/`, `tools/`. Нові файли даних додавати в його `$Keep`.
- Помилки Gemini показувати чесно, людською мовою (ліміт безкоштовного ключа, неправильний ключ, 503).
- Жодних справжніх ключів у коді й тестах; фейковий ключ у тестах збирати під час виконання
  (інакше спрацьовує secret scanning GitHub).
- `subprocess.run` завжди з `timeout=`; шляхи через `pathlib`; захист від path traversal через
  `Path.resolve().is_relative_to()`.
- Тести ізолюють БД і папки (`tests/conftest.py`); нові тести не мають писати в справжні `web_output/` чи лог.
- Після змін бекенду на ноутбуці потрібен перезапуск студії і Ctrl+F5 у браузері.
- Папка `tools/` належить інсталятору і в `.gitignore`; скрипти для розробки кладемо в `scripts/`.
