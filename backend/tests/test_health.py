from __future__ import annotations


def test_health_ok(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["database"]["status"] == "ok"


def test_health_providers_never_leaks_secrets(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-super-secret-value")
    r = client.get("/api/health/providers")
    assert r.status_code == 200
    assert "sk-super-secret-value" not in r.text
    assert r.json()["providers"]["llm"]["status"] in ("configured", "not_configured")
