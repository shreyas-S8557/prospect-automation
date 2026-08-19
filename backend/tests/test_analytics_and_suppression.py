from __future__ import annotations

from tests.helpers import seed_qualified_leads


def test_dashboard_stats_empty(client):
    r = client.get("/api/dashboard/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["total_campaigns"] == 0


def test_campaign_stats_and_funnel(client):
    campaign = client.post(
        "/api/campaigns", json={"campaign_name": "Analytics Test", "target_leads": 10}
    ).json()
    seed_qualified_leads(campaign["campaign_id"], count=4)

    stats = client.get(f"/api/campaigns/{campaign['campaign_id']}/stats").json()
    assert stats["discovered"] == 4
    assert stats["qualified"] == 4

    funnel = client.get(f"/api/campaigns/{campaign['campaign_id']}/funnel").json()
    stage_counts = {s["stage"]: s["count"] for s in funnel["stages"]}
    assert stage_counts["discovered"] == 4
    assert stage_counts["qualified"] == 4
    assert stage_counts["sent"] == 0


def test_stats_404_for_unknown_campaign(client):
    r = client.get("/api/campaigns/does-not-exist/stats")
    assert r.status_code == 404


def test_suppression_create_list_delete(client):
    r = client.post(
        "/api/settings/suppressions", json={"email": "test@example.com", "reason": "opt-out"}
    )
    assert r.status_code == 201

    r2 = client.get("/api/settings/suppressions")
    emails = [s["email_normalized"] for s in r2.json()]
    assert "test@example.com" in emails

    r3 = client.delete("/api/settings/suppressions/test@example.com")
    assert r3.status_code == 204

    r4 = client.get("/api/settings/suppressions")
    emails_after = [s["email_normalized"] for s in r4.json()]
    assert "test@example.com" not in emails_after


def test_settings_reports_provider_status_without_secrets(client, monkeypatch):
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "super-secret-app-password")
    r = client.get("/api/settings")
    assert r.status_code == 200
    assert "super-secret-app-password" not in r.text
