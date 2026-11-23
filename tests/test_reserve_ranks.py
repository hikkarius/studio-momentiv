"""«Запас» (weaker clips kept out of «Нові») and honest rank scores."""
import uuid

from fastapi.testclient import TestClient

from backend import db
from backend.server import app

client = TestClient(app)


def _project(n_clips: int, duration: float, scores=None):
    pid = f"test_reserve_{uuid.uuid4().hex[:8]}"
    db.create_project(pid, "Подкаст", "/tmp/none.mp4", duration)
    scores = scores or [90 - i for i in range(n_clips)]
    db.save_candidate_clips(pid, [{"start": i * 30.0, "end": i * 30.0 + 20, "title": f"К{i}", "score": s} for i, s in enumerate(scores)])
    return pid


def test_shelf_size():
    assert db.new_shelf_size(300) == 5 and db.new_shelf_size(902) == 10 and db.new_shelf_size(3 * 3600) == 15


def test_save_puts_weaker_clips_in_reserve():
    # 15 min video → 10 best clips stay in «Нові», the other 25 go to «Запас»
    scores = [70 + (i * 7) % 29 for i in range(35)]
    pid = _project(35, 902.0, scores)
    clips = db.get_candidate_clips(pid)
    new = [c for c in clips if not c["reserve"]]
    assert len(new) == 10
    assert min(c["virality_score"] for c in new) >= max(c["virality_score"] for c in clips if c["reserve"])


def test_patch_reserve_and_status_leaves_reserve():
    pid = _project(8, 300.0)  # 5 new, 3 reserve
    c6 = next(c for c in db.get_candidate_clips(pid) if c["clip_index"] == 6)
    assert c6["reserve"] == 1
    res = client.patch(f"/api/projects/{pid}/clips/6", json={"reserve": False}).json()["clip"]
    assert res["reserve"] == 0 and "rank_score" in res
    assert client.patch(f"/api/projects/{pid}/clips/7", json={"review_status": "keep"}).json()["clip"]["reserve"] == 0


def test_migration_keeps_reviewed_clips():
    pid = _project(8, 300.0)
    db.update_clip_fields(pid, 8, review_status="drop")  # the weakest one, already reviewed
    db.update_clip_fields(pid, 7, review_status="keep")
    with db.get_db() as conn:
        conn.execute("ALTER TABLE candidate_clips DROP COLUMN reserve")
    db.init_db()
    by = {c["clip_index"]: c for c in db.get_candidate_clips(pid)}
    assert [by[i]["reserve"] for i in range(1, 7)] == [0, 0, 0, 0, 0, 1]
    assert by[7]["reserve"] == 0 and by[8]["reserve"] == 0  # reviewed clips are never touched
    assert by[7]["review_status"] == "keep" and by[8]["review_status"] == "drop"


def test_rank_scores_and_tiers():
    clips = db.with_ranks([{"virality_score": s} for s in [95, 95, 90, 88, 85, 85, 85, 80, 70, 60]])
    # 7 distinct AI scores spread evenly from 99 to 40; equal AI scores share one
    assert [c["rank_score"] for c in clips] == [99, 99, 89, 79, 70, 70, 70, 60, 50, 40]
    # top: best 20 % but at least 3 → the two 95s and the 90; good: the next 30 %
    assert [c["tier"] for c in clips] == ["top", "top", "top", "good", "good", "good", "good", "", "", ""]
    assert db.with_ranks([{"virality_score": 80}])[0]["rank_score"] == 99
    assert db.with_ranks([]) == []
