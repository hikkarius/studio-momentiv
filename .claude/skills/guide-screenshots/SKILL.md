---
name: guide-screenshots
description: Refresh the screenshots and walkthrough video of the friends guide after the UI changes, using scripts/guide_capture.py.
---

# Оновити скріншоти й відео гайду

Гайд для друзів показує перший запуск (ключ Gemini), головну, історію, монтажний стіл і скачування ZIP.
Коли ці екрани змінюються, онови картинки й відео.

## Підготовка (хмарна сесія)

Оточення як у скілі `run-studio`. Для **відео** версія pip-пакета `playwright` має відповідати
передвстановленим браузерам: подивись `ls /opt/pw-browsers` (`ffmpeg-NNNN`, `chromium-NNNN`) і постав
версію playwright, яка їх очікує. Інакше запис мовчки не створює файлів. Для скріншотів версія не важлива.
Потрібен системний `ffmpeg`.

## Знімати

```bash
.venv/bin/python scripts/guide_capture.py OUT/shots --shots   # 01-welcome … 10-zip, PNG 2x
.venv/bin/python scripts/guide_capture.py OUT/video           # OUT/video/raw/*.webm, ~27 с
```

`OUT` — папка в scratchpad, не в репозиторії. Скрипт не потребує ключа Gemini: перевірку ключа,
`/api/health` (зелена RTX 3060) і транскрипцію він підміняє.

Якщо змінились id елементів (`#welcomeKey`, `#urlInput`, `#edKeep`, `#edZip` тощо), спершу онови сценарій
у `scripts/guide_capture.py`.

## Обробка

- PNG → JPEG 1600×1000, якість 84 (Pillow).
- Відео: `ffmpeg -i raw/*.webm -c:v libx264 -preset slow -crf 26 -pix_fmt yuv420p -movflags +faststart walkthrough.mp4`
- Обкладинка: `ffmpeg -ss 1 -i walkthrough.mp4 -frames:v 1 -q:v 4 img/poster.jpg`

## Перевірити й опублікувати

Переглянь кожен кадр: немає порожніх екранів, обрізаного тексту, англійських заглушок. Потім онови
сторінку гайду (Artifact, посилання в пам'яті проєкту) тими самими шляхами до файлів і скажи автору,
які кадри змінилися.
