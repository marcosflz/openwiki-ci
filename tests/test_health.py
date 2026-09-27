"""Health endpoint tests."""

from __future__ import annotations


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["version"]
    assert payload["pool"]["workers"] == 1


def test_root_redirects_to_docs(client):
    response = client.get("/")
    assert response.status_code == 200
    assert response.json()["docs"] == "/docs"
