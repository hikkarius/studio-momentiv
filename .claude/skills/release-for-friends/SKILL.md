---
name: release-for-friends
description: Ship a new version of the studio to friends - bump version.json, check the installer and updater, merge so GitHub Actions publishes Studio-Momentiv.zip. Use for any release or hotfix.
---

# Реліз для друзів

Друзі ставлять студію з `Studio-Momentiv.zip` останнього GitHub Release і оновлюються кнопкою «⬆ Оновити».
Реліз збирає `.github/workflows/release.yml`, коли на `main` змінюється `version.json`.

## Кроки

1. **Тести зелені**: `pytest tests -q` (див. скіл `run-studio`), разом з `tests/e2e`.
2. **Номер версії** у `version.json` (`"version": "X.Y.Z"`): виправлення → Z+1, нова можливість → Y+1.
   Нічого більше в цьому файлі не міняти (`repo`, `asset` читає кнопка «Оновити»).
3. **Дані друзів у безпеці**: якщо додано новий файл чи папку з даними користувача, додай їх у `$Keep`
   у `installer/update.ps1` і в `.gitignore`.
4. **Що потрапить в архів**: workflow бере `git ls-files` і викидає `tests/`, `.github/`, `.agents/`,
   `.antigravity/`, `.claude/`, `scripts/`, `CLAUDE.md` та кілька файлів розробки. Новий файл тільки для
   розробки додай у той `grep -zvE` список. Новий файл, потрібний студії, має бути закомічений
   (не в `.gitignore`), інакше його не буде в архіві.
5. **Нові залежності**: потрібні всім → `requirements-base.txt`; тільки для NVIDIA → `requirements-gpu.txt`;
   тільки для тестів → `requirements-dev.txt` / `requirements.txt`. Інсталятор читає перші два.
6. **Інсталятор змінився?** Попроси автора перевірити на ноутбуці в чистій папці:
   `powershell -ExecutionPolicy Bypass -File installer\install.ps1 -Quiet`, потім запуск ярликом.
   PowerShell 5: файли `.ps1` зберігати в UTF-8 з BOM (там кирилиця).
7. **PR → merge в `main`**. Після злиття перевір вкладку Actions: job «Release for friends» зелений,
   з'явився Release `vX.Y.Z` з `Studio-Momentiv.zip`. Якщо такий реліз уже є, workflow нічого не робить:
   тоді треба підняти номер ще раз.
8. **Перевірка оновлення**: на ноутбуці зі старішою версією має з'явитися «⬆ Оновити», після нього —
   нова версія, а відео, ключ і налаштування на місці.

## Повідомлення автору

Коротко українською: номер версії, що змінилося для друзів (простими словами, 2–4 пункти), посилання на
Release. Цей текст автор може переслати друзям.
