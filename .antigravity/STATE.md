# STATE.md — Поточний стан проєкту та Реєстр задач

> **Останнє оновлення:** 2026-11-18  
> **Відповідальна роль:** `[PM]` Project Manager  
> **Поточний етап:** Усі 3 заплановані спринти аудиту (**Sprint 1 + Sprint 2 + Sprint 3**) — **ЗАВЕРШЕНО (APPROVED)**

---

## 1. Стан системи (Health Snapshot)

| Метрика | Значення | Статус |
|---|---|---|
| **Git Status** | Зміни **Sprint 1–3** зафіксовано, **Sprint 4–5** готові до коміту | 🟢 Clean |
| **Pytest** | `160 passed` (19.76s) — додано тести для SRT та Trim | 🟢 Pass |
| **Code Coverage** | `88%` (1955 stmts, 233 missed; ціль ≥ 85%) | 🟢 Exceeds target |
| **review_agent.py --e2e** | `0 blockers, 0 warnings` — ВЕРДИКТ: КОД БЕЗДОГАННИЙ | 🟢 Pass |
| **Локальне GPU-зондування** | `NVENC=True, CUDA=True` (NVIDIA GeForce RTX 4050, кешовано в `AppState`) | 🟢 Optimal |
| **Чистота робочих папок** | `web_output/` чиста; ротація логів `5 MB × 3` | 🟢 Clean |

---

## 2. Завершені етапи

- [x] **Етап 1: Multi-Agent Full Project Scan & Audit**: Інвентаризація, SWOT-аналіз, ініціалізація `.antigravity/`.
- [x] **Sprint 1 — Security & Critical Bugfixes** (`APPROVED`).
- [x] **Sprint 2 — Performance, Concurrency & Stability** (`APPROVED`).
- [x] **Sprint 3 — Tech Debt Cleanup, Docs Sync & QA Hardening** (`APPROVED`).
- [x] **Sprint 4 — Core Stabilization, Purge Cloud Audio & UI Session Lifecycle** (`APPROVED`):
  - [x] `PURGE-01`: Повне видалення `_analyze_via_gemini_multimodal_audio`, чанкінгу `audio_chunk_*` та карток вибору рушія в інтерфейсі. Пайплайн стандартизовано на `whisper_local` + Gemini Text Virality Reasoning.
  - [x] `UX-01`: Централізований `resetStudioState()`: зупинка відеоплеєра, очищення пам'яті `sourceVideo.src`, миттєве зникнення старих моментів при виборі нового відео, кнопка «✕ Скинути вибір» в активному банері, toggle-деактивація в історії.
  - [x] `WHISPER-01`: Faster-Whisper тюнінг: `condition_on_previous_text=False`, точний VAD (`min_silence_duration_ms=400`, `speech_pad_ms=200`), `beam_size: 5` у `config.yaml` / `config_schema.py`, посилена пунктуація для української мови.
  - [x] `TEST-01`: Усі 150 тестів успішно пройдено, тестове покриття зросло до **88%**, `review_agent.py --e2e` видає вердикт "БЕЗДОГАННИЙ", зафіксовано **ADR-008**.
- [x] **Sprint 5 — Dynamic Subtitles Export (.SRT) & Interactive Clip Trimming** (`APPROVED`):
  - [x] `FEAT-01`: Модуль `subtitles_generator.py`: генерація відносних нульових таймкодів `00:00:00,000` на базі кешованих `words` Whisper; динамічне розбиття по 2–4 слова на рядок (Shorts/CapCut style), пунктуаційне сегментування та інтерполяційний fallback на `segments`. Додано ендпоінти `GET /api/projects/{id}/clips/{idx}/export-srt` та `GET /api/projects/{id}/export-all-subtitles` (ZIP).
  - [x] `FEAT-02`: Модальне вікно точного підрізання `trimModal`: прев'ю-плеєр, live-індикатор часу, кнопки швидкого кроку `±0.2с`, `±1с`, встановлення за поточним часом плеєра, чекбокс авто-зупинки на кінці кліпу, `PUT /api/projects/{id}/clips/{idx}/trim` з оновленням у SQLite і скиданням застарілих кешів нарізок. Додано кнопки в кожну картку моменту та верхню панель експорту.
  - [x] `TEST-02`: Написано `tests/test_subtitles.py` та розширено `tests/test_studio_backend.py` (160 тестів, покриття **88%**, subtitles_generator 93%, вердикт `review_agent.py --e2e` — "БЕЗДОГАННИЙ"), зафіксовано **ADR-009**.

---

## 3. Активна задача

- **Спринт 5**: **Успішно завершено**. Студія повністю протестована, готова до живого використання користувачем або фіксації в `git commit`.
- **Узгоджений регламент роботи (Workflow Contract):**
  1. Обговорення ідеї + уточнюючі питання (`[PM]` / `[Architect]`) — *виконано*.
  2. Пропозиція варіантів реалізації з обґрунтуванням вибору — *виконано*.
  3. **План реалізації на погодження користувачем** — *виконано*.
  4. Виконання за конвеєром `[Dev]` → `[Auditor]` → `[Reviewer]` + синхронізація `.antigravity/` — *виконано*.

---

## 4. Продуктовий контекст і Беклог наступних фіч (User Roadmap)

- **Цільовий режим**: локальний інструмент під Windows для особистого використання (без публічного деплою/авторизації на цьому етапі).
- **Основний рушій**: `whisper_local` (**Faster-Whisper CUDA**) — єдиний рушій, оптимізований під максимальну точність.
- **Поточний беклог (майбутні ідеї для обговорення)**:
  - Пакетування кореневих модулів ядра у структуру `core/` (при подальшому масштабуванні).
  - Фоновий SSE прогрес-бар для довгих завантажень через `/api/upload-url`.
  - Можливість кастомного перейменування заголовків моментів прямо в UI.

---

## 5. Журнал змін статусу (Activity Log)

- **2026-11-18**: `[Architect]`, `[Auditor]`, `[PM]` завершили Етап 1 (Full Project Scan). Зафіксовано 18 задач беклогу (7 High, 7 Medium, 4 Low). Створено `.antigravity/`.
- **2026-11-18**: **Sprint 1** (`BUG-01..03`, `SEC-01..03`, `TEST-01`) — **APPROVED** (`158 passed`, 80% coverage, `CUDA=True` на RTX 4050).
- **2026-11-18**: **Sprint 2** (`PERF-01..02`, `BUG-04..05`, `DB-01`, `SEC-04`, `OPS-01`) — **APPROVED** (`166 passed`, 81% coverage).
- **2026-11-18**: **Sprint 3** (`DEBT-01..04`) — **APPROVED** (`150 passed`, **87% coverage**, 0 blockers/warnings із посиленим `review_agent.py`, E2E OK).
