---
name: run-studio
description: Launch the studio, run its tests and check a change in the real UI (cloud session or the Windows laptop). Use before saying a change works.
---

# Запуск і перевірка студії

## 1. Оточення

Хмарна сесія (Linux), один раз за сесію:

```bash
uv venv -p 3.12 .venv
uv pip install -p .venv -r requirements-base.txt httpx pytest pytest-cov playwright
```

GPU-бібліотеки (`requirements-gpu.txt`) у хмарі не потрібні: Whisper там іде на CPU, а для перевірки
інтерфейсу транскрипцію підмінюють (див. п. 3).

Windows-ноутбук автора: `C:\studio_momentiv\.venv` уже є. Команди ті самі, але `.venv\Scripts\python.exe`.

## 2. Тести

```bash
.venv/bin/python -m pytest tests -q -o addopts=""
```

- Під час виправлень перезапускай лише тести, що впали: `pytest tests/test_x.py::test_name`.
- `tests/e2e/` — справжній браузер (Playwright). Якщо Chromium не знайдено, вони пропускаються, а не падають:
  перевір у звіті, що вони справді пройшли (`-rs` показує пропуски).
- Ніколи не вимикай і не пропускай тест, щоб стало зелено.

## 3. Подивитися зміну в інтерфейсі

Найпростіше: взяти готову підготовку з `tests/e2e/conftest.py` або `scripts/guide_capture.py` — вони
запускають `backend.server.app` через uvicorn на вільному порту з ізольованою БД, згенерованим ffmpeg
тестовим відео і підміненою транскрипцією, без ключа Gemini. Після цього Playwright:

- `chromium.launch()` (у хмарі без параметрів, або `executable_path="/opt/pw-browsers/chromium"`);
- вікно 1280×800; тема: `localStorage.setItem('studio-theme', 'dark' | 'light')` і reload;
- перший запуск без ключа показує `#welcomeScreen`; щоб він не заважав, підміни
  `config_loader.ensure_gemini_api_key` або збережи фейковий ключ через сам екран;
- скріншоти для себе — `device_scale_factor=1`, для людини — `2`.

Перевір обидві теми («Гараж» світла, «Чорне золото» темна) і ширину телефона (390 px), якщо мінявся CSS.

## 4. Справжній запуск

```bash
.venv/bin/python run_studio.py   # http://127.0.0.1:8000
```

Справжній аналіз потребує `GEMINI_API_KEY` у `.env`; у хмарі його немає, тож повний цикл
(посилання → Whisper → Gemini) перевіряє автор на ноутбуці. Скажи йому, що саме натиснути і що має
з'явитися, і нагадай: після змін бекенду перезапустити студію і натиснути Ctrl+F5.

## 5. Звіт

Пиши, що саме перевірено (тести: скільки пройшло; які екрани відкрито; скріншот), і чого не перевірено
(наприклад, GPU або справжній Gemini).
