"""API smoke test — health, stage list, and request validation."""

import pytest

httpx = pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from framefrog.api import app  # noqa: E402


def test_health():
    client = TestClient(app)
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "audio_cleanup" in body["stages"]


def test_stages():
    client = TestClient(app)
    r = client.get("/stages")
    assert r.status_code == 200
    assert len(r.json()["stages"]) == 7


def test_post_requires_input():
    client = TestClient(app)
    r = client.post("/post_production", json={"stages": ["scene_detect"]})
    assert r.status_code == 422


def test_missing_job_404():
    client = TestClient(app)
    assert client.get("/jobs/fj_doesnotexist").status_code == 404
