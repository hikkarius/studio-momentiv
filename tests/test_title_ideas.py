"""Three hook titles from the AI for the 9:16 title plate, and learning from the author's picks."""
import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import title_ideas
from backend import db, server
from backend.server import app

client = TestClient(app)

GOOD = json.dumps([
    {"kind": "цифра", "text": "90% людей не знають цього про гроші"},
    {"kind": "питання", "text": "Чи можна стати мільйонером чесно?"},
    {"kind": "інтрига", "text": "«Він сказав те, що всі бояться визнати»"},
], ensure_ascii=False)


class FakeModels:
    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts = []

    def generate_content(self, model, contents, config):
        self.prompts.append(contents)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return SimpleNamespace(text=a)


def fake_client(*answers):
    return SimpleNamespace(models=FakeModels(answers))


def _project():
    pid = f"test_titles_{uuid.uuid4().hex[:8]}"
    db.create_project(pid, "Подкаст", "/tmp/none.mp4", 600)
    db.save_candidate_clips(pid, [{"start": 10.0, "end": 40.0, "title": "ЧЕСНИЙ БІЗНЕС", "score": 90}])
    return pid


def test_parse_variants_cleans_and_orders():
    raw = json.dumps({"titles": [
        {"kind": "інтрига", "text": "  Він   сказав  "},
        {"kind": "Цифра", "text": "90% людей"},
        {"kind": "цифра", "text": "дубль іншого виду"},
        {"kind": "мем", "text": "невідомий вид"},
    ]}, ensure_ascii=False)
    assert title_ideas.parse_variants(raw) == [
        {"kind": "цифра", "text": "90% людей"},
        {"kind": "інтрига", "text": "Він сказав"},
    ]


def test_prompt_shows_the_authors_picks():
    taste = {"recent": [{"kind": "питання", "text": "А ти б зміг?"}], "counts": {"питання": 5, "цифра": 1}, "total": 6}
    p = title_ideas.build_prompt("текст кліпу", "НАЗВА", "uk", taste)
    assert "А ти б зміг?" in p and "«питання»" in p
    assert "раніше обрав" not in title_ideas.build_prompt("текст", "НАЗВА", "uk", None)


def test_suggest_titles_tries_next_model(monkeypatch):
    monkeypatch.setattr(title_ideas, "CANDIDATE_MODELS", ["m1", "m2"])
    fc = fake_client(RuntimeError("503 UNAVAILABLE"), GOOD)
    out = title_ideas.suggest_titles("слова кліпу", "НАЗВА", client=fc)
    assert [v["kind"] for v in out] == ["цифра", "питання", "інтрига"]
    assert out[2]["text"] == "Він сказав те, що всі бояться визнати"


@pytest.mark.parametrize("err, msg", [
    ("429 RESOURCE_EXHAUSTED quota", title_ideas.QUOTA_MESSAGE),
    ("API_KEY_INVALID", title_ideas.BAD_KEY_MESSAGE),
    ("503 UNAVAILABLE", title_ideas.BUSY_MESSAGE),
])
def test_suggest_titles_honest_errors(monkeypatch, err, msg):
    monkeypatch.setattr(title_ideas, "CANDIDATE_MODELS", ["m1", "m2"])
    with pytest.raises(title_ideas.TitleIdeasError) as e:
        title_ideas.suggest_titles("слова", "НАЗВА", client=fake_client(RuntimeError(err), RuntimeError(err)))
    assert str(e.value) == msg


def test_empty_clip_text_is_not_sent():
    with pytest.raises(title_ideas.TitleIdeasError):
        title_ideas.suggest_titles("  ", "НАЗВА", client=fake_client())


def test_title_ideas_endpoint_stores_variants(monkeypatch):
    pid = _project()
    seen = {}
    monkeypatch.setattr(server, "load_transcript_cache", lambda *a, **k: (
        [], [{"start": 5.0, "end": 5.5, "word": "до"}, {"start": 12.0, "end": 12.4, "word": "Чесно"}, {"start": 13.0, "end": 13.3, "word": "кажучи"}], "uk"))

    def fake_suggest(text, title, lang, taste=None, client=None):
        seen.update(text=text, title=title, lang=lang)
        return [{"kind": "цифра", "text": "90% людей"}]
    monkeypatch.setattr(server.title_ideas, "suggest_titles", fake_suggest)
    r = client.post(f"/api/projects/{pid}/clips/1/title-ideas")
    assert r.status_code == 200
    assert seen == {"text": "Чесно кажучи", "title": "ЧЕСНИЙ БІЗНЕС", "lang": "uk"}
    assert db.get_candidate_clips(pid)[0]["title_variants"] == [{"kind": "цифра", "text": "90% людей"}]


def test_title_ideas_endpoint_error_is_plain_text(monkeypatch):
    pid = _project()
    monkeypatch.setattr(server, "load_transcript_cache", lambda *a, **k: ([], [{"start": 12.0, "end": 12.4, "word": "слово"}], "uk"))

    def boom(*a, **k):
        raise title_ideas.TitleIdeasError(title_ideas.QUOTA_MESSAGE)
    monkeypatch.setattr(server.title_ideas, "suggest_titles", boom)
    r = client.post(f"/api/projects/{pid}/clips/1/title-ideas")
    assert r.status_code == 503 and r.json()["detail"] == title_ideas.QUOTA_MESSAGE


def test_title_choice_one_per_clip_and_counts():
    pid = _project()
    assert client.post(f"/api/projects/{pid}/clips/1/title-choice", json={"kind": "цифра", "text": "90% людей"}).json()["taste"] == 1
    r = client.post(f"/api/projects/{pid}/clips/1/title-choice", json={"kind": "питання", "text": "А ти?"})
    assert r.json()["taste"] == 1  # a new pick for the same clip replaces the old one
    t = db.title_choices()
    assert t["recent"] == [{"kind": "питання", "text": "А ти?"}] and t["counts"] == {"питання": 1}
    db.add_title_choice(pid, 1, "щось дивне", "Свій текст")
    assert db.title_choices()["recent"][0]["kind"] == "свій"
    assert client.post(f"/api/projects/{pid}/clips/9/title-choice", json={"kind": "цифра", "text": "x"}).status_code == 404
