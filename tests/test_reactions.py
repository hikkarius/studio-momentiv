"""
tests/test_reactions.py — «Моменти за реакцією»: loud moments and chat spikes as hints for Gemini.
"""
import json
import shutil
import subprocess

import pytest

import reactions

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")


def test_peaks_finds_jumps_above_the_minute_around():
    levels = [-30.0] * 300
    levels[100] = -12.0          # a shout
    levels[101] = -14.0          # same moment
    levels[200] = -20.0          # a smaller one
    levels[250] = -27.0          # too small
    out = reactions.peaks(levels)
    assert [t for t, _ in out] == [100.0, 200.0]
    assert out[0][1] == 18.0
    assert reactions.peaks([-30.0] * 5) == []
    assert reactions.peaks([-90.0] * 100) == []  # silence is not a reaction


@needs_ffmpeg
def test_loudness_per_second_real(tmp_path):
    src = tmp_path / "a.mp4"
    # quiet tone, a loud second at 6 s, quiet again
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=12",
                    "-af", "volume='if(between(t,6,7),1,0.05)':eval=frame", str(src)], check=True, timeout=60)
    levels = reactions.loudness_per_second(str(src))
    assert 11 <= len(levels) <= 13
    assert max(range(len(levels)), key=lambda i: levels[i]) == 6
    assert reactions.loudness_peaks(str(src))[0][0] == 6.0


def test_chat_times_from_youtube_and_twitch_files(tmp_path):
    yt = tmp_path / "chat.live_chat.json"
    yt.write_text("\n".join(json.dumps({"replayChatItemAction": {"actions": [{}], "videoOffsetTimeMsec": str(ms)}})
                            for ms in (1500, 2500, 64000)) + "\nnot json\n", encoding="utf-8")
    assert reactions.chat_times_from_file(str(yt)) == [1.5, 2.5, 64.0]
    tw = tmp_path / "chat.rechat.json"
    tw.write_text(json.dumps({"comments": [{"content_offset_seconds": 12.0}, {"contentOffsetSeconds": 3}]}), encoding="utf-8")
    assert reactions.chat_times_from_file(str(tw)) == [3.0, 12.0]


def test_chat_spikes():
    times = [i * 2.0 for i in range(300)]          # 5 messages per 10 s
    times += [300 + i * 0.3 for i in range(30)]     # 30+ in one window
    out = reactions.chat_spikes(times)
    assert len(out) == 1 and out[0][0] == 300.0 and out[0][1] >= 30
    assert reactions.chat_spikes([1.0, 2.0]) == []


def test_fetch_chat_only_for_stream_sites(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("no yt-dlp for other sites"))
    assert reactions.fetch_chat("https://vimeo.com/123", str(tmp_path / "c.json")) == 0


def test_fetch_chat_keeps_only_times(tmp_path, monkeypatch):
    def fake_run(cmd, **k):
        out = cmd[cmd.index("-o") + 1].replace("%(ext)s", "live_chat.json")
        with open(out, "w", encoding="utf-8") as f:
            for ms in (1000, 2000):
                f.write(json.dumps({"replayChatItemAction": {"videoOffsetTimeMsec": str(ms)}}) + "\n")
    monkeypatch.setattr(subprocess, "run", fake_run)
    dest = tmp_path / "p" / "chat.json"
    assert reactions.fetch_chat("https://www.twitch.tv/videos/1", str(dest)) == 2
    assert reactions.load_chat(str(dest)) == [1.0, 2.0]
    assert reactions.load_chat(str(tmp_path / "none.json")) == []


def test_mark_segments_marks_copies_only():
    segs = [{"start": 0.0, "end": 5.0, "text": "привіт"}, {"start": 5.0, "end": 9.0, "text": "А-А-А!"},
            {"start": 100.0, "end": 104.0, "text": "далі"}]
    marked, n = reactions.mark_segments(segs, loud=[(6.0, 14.0)], chat=[(0.0, 40), (500.0, 50)])
    assert n == 2
    assert marked[1]["text"].startswith("[реакція: різкий сплеск гучності (+14 дБ")
    assert marked[1]["text"].endswith("А-А-А!")
    assert "чат вибухнув (40" in marked[0]["text"]  # the window 0-10 s, centred at 5 s
    assert marked[2]["text"] == "далі"              # 500 s is too far from any line
    assert segs[1]["text"] == "А-А-А!"              # the real transcript is untouched


def test_prompt_explains_marks_only_when_present(monkeypatch):
    import ai_selector

    class Resp:
        text = '[{"start": 0, "end": 20, "title": "T", "hook_text": "h"}]'

    prompts = []

    class Models:
        def generate_content(self, model, contents, config):
            prompts.append(contents)
            return Resp()

    class Client:
        models = Models()

    segs = [{"start": 0.0, "end": 30.0, "text": "звичайний текст"}]
    ai_selector._call_gemini_single_window(Client(), segs, 1, 15, 60, "viral_hooks", "uk")
    marked, _ = reactions.mark_segments(segs, loud=[(3.0, 12.0)])
    ai_selector._call_gemini_single_window(Client(), marked, 1, 15, 60, "viral_hooks", "uk")
    assert "Це запис стріму" not in prompts[0] and "Це запис стріму" in prompts[-1]
