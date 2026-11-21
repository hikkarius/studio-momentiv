"""Interface files must be re-checked by the browser after an update (no stale look after «Оновити»)."""
from fastapi.testclient import TestClient

from backend import server


def test_interface_files_are_revalidated():
    client = TestClient(server.app)
    for path in ("/", "/js/home.js", "/css/style.css"):
        res = client.get(path)
        assert res.status_code == 200, path
        assert res.headers.get("cache-control") == "no-cache", path


def test_unchanged_js_comes_back_as_304():
    client = TestClient(server.app)
    first = client.get("/js/home.js")
    etag = first.headers.get("etag")
    assert etag
    again = client.get("/js/home.js", headers={"If-None-Match": etag})
    assert again.status_code == 304


def test_api_responses_keep_their_own_caching():
    client = TestClient(server.app)
    res = client.get("/api/health")
    assert res.headers.get("cache-control") != "no-cache"
