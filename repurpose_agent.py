"""
repurpose_agent.py — CLI entry point for AI Video Moments Analyzer.

Usage:
    python repurpose_agent.py <video.mp4> [clip_count] [output_dir]
"""
import argparse
import json
import os
import sys

from utils import setup_console_encoding, get_video_duration
setup_console_encoding()

from constants import FilterMode, resolve_filter_mode
from core import analyze_and_find_clips, app_config, transcribe_video, validate_clips, log


def run_agent(
    input_video_path: str,
    clips_count: int = 5,
    output_dir: str = "output_moments",
    filter_mode: str = FilterMode.VIRAL_HOOKS,
    min_len: int | None = None,
    max_len: int | None = None,
) -> list[dict]:
    """Runs audio transcription and Gemini viral moments analysis."""
    if not os.path.exists(input_video_path):
        raise FileNotFoundError(f"Файл не знайдено: {input_video_path}")

    min_len = min_len if min_len is not None else app_config.clips.default_min_len
    max_len = max_len if max_len is not None else app_config.clips.default_max_len

    os.makedirs(output_dir, exist_ok=True)
    duration = get_video_duration(input_video_path)
    log.info(f"Аналіз відео '{input_video_path}' (тривалість: {duration:.1f}с)")

    # 1. Transcribe with Whisper
    log.info("Розпізнавання аудіо через Faster-Whisper...")
    segments, words, detected_lang, from_cache = transcribe_video(input_video_path)
    if not segments:
        raise RuntimeError("Не вдалося отримати транскрипцію аудіо.")

    # 2. Analyze with Gemini AI
    log.info(f"ШІ-пошук вірусних моментів ({clips_count} фрагментів)...")
    raw_clips = analyze_and_find_clips(
        client=None,
        segments=segments,
        count=clips_count,
        min_len=min_len,
        max_len=max_len,
        filter_mode=resolve_filter_mode(filter_mode),
        detected_lang=detected_lang,
    )
    valid_clips = validate_clips(raw_clips, duration, min_len=min_len, max_len=max_len)

    # 3. Export metadata
    txt_path = os.path.join(output_dir, "metadata.txt")
    json_path = os.path.join(output_dir, "metadata.json")

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(valid_clips, f, ensure_ascii=False, indent=2)

    with open(txt_path, "w", encoding="utf-8") as f:
        f.write("====================================================\n")
        f.write("      ВІРУСНІ МОМЕНТИ ТА МЕТАДАНІ ДЛЯ ПУБЛІКАЦІЇ     \n")
        f.write("====================================================\n\n")
        for i, c in enumerate(valid_clips, 1):
            s_m, s_s = divmod(int(c['start']), 60)
            e_m, e_s = divmod(int(c['end']), 60)
            dur = int(c['end'] - c['start'])
            f.write(f"--- КЛІП #{i}: {c.get('title', 'Без назви')} ---\n")
            f.write(f"⏱️ Таймкод: {s_m:02d}:{s_s:02d} - {e_m:02d}:{e_s:02d} ({dur} сек)\n")
            f.write(f"🔥 Вірусний бал: {c.get('score', 85)}/100\n")
            f.write(f"🎯 Хук (початок): {c.get('hook', '')}\n")
            f.write(f"💡 Чому це вірусно: {c.get('reason', '')}\n")
            f.write(f"📝 Опис: {c.get('caption', '')}\n")
            f.write(f"🏷️ Хештеги: {' '.join(c.get('hashtags', []))}\n\n")

    log.info(f"Готово! Знайдено {len(valid_clips)} моментів. Збережено в {output_dir}")
    return valid_clips


def main():
    parser = argparse.ArgumentParser(description="AI Video Moments Analyzer — Пошук вірусних моментів.")
    parser.add_argument("input_video", help="Шлях до відеофайлу")
    parser.add_argument("-c", "--clips", type=int, default=5, help="Кількість кліпів (за замовчуванням: 5)")
    parser.add_argument("-o", "--output-dir", default="output_moments", help="Папка виводу")
    parser.add_argument("-f", "--filter", default=FilterMode.VIRAL_HOOKS, help="Стиль відбору")

    args = parser.parse_args()
    clips = run_agent(args.input_video, clips_count=args.clips, output_dir=args.output_dir, filter_mode=args.filter)
    print(f"\nЗнайдено {len(clips)} моментів:")
    for i, c in enumerate(clips, 1):
        print(f"#{i} [{c['start']:.1f}s - {c['end']:.1f}s] {c.get('title')}: {c.get('hook')}")


if __name__ == "__main__":
    main()
