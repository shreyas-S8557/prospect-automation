from __future__ import annotations

import time

from tests.helpers import seed_qualified_leads


def _wait(client, job_id, timeout=10):
    deadline = time.time() + timeout
    body = None
    while time.time() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("completed", "failed", "cancelled"):
            return body
        time.sleep(0.02)
    return body


def _approved_campaign(client, count=2):
    campaign = client.post(
        "/api/campaigns",
        json={
            "campaign_name": "Send Test",
            "target_leads": 10,
            "sending_enabled": True,
            "send_mode": "dry_run",
        },
    ).json()
    seed_qualified_leads(campaign["campaign_id"], count=count)
    for stage in ("find-emails", "validate-emails", "generate-emails"):
        job = client.post(f"/api/campaigns/{campaign['campaign_id']}/{stage}").json()
        _wait(client, job["job_id"])
    client.post(f"/api/campaigns/{campaign['campaign_id']}/emails/approve-all")
    return campaign


def test_dry_run_send_never_touches_network_and_completes(client):
    campaign = _approved_campaign(client)
    r = client.post(f"/api/campaigns/{campaign['campaign_id']}/send")
    assert r.status_code == 200
    final = _wait(client, r.json()["job_id"])
    assert final["status"] == "completed"
    assert final["result"]["send_mode"] == "dry_run"

    summary = client.get(f"/api/campaigns/{campaign['campaign_id']}/sending-summary").json()
    assert summary["sent"] >= 1


def test_send_requires_sending_enabled(client):
    campaign = client.post(
        "/api/campaigns",
        json={"campaign_name": "Not Enabled", "target_leads": 5, "sending_enabled": False},
    ).json()
    r = client.post(f"/api/campaigns/{campaign['campaign_id']}/send")
    assert r.status_code == 409


def test_live_send_blocked_without_explicit_opt_in(client):
    campaign = client.post(
        "/api/campaigns",
        json={
            "campaign_name": "Live Attempt",
            "target_leads": 5,
            "sending_enabled": True,
            "send_mode": "live",
        },
    ).json()
    r = client.post(f"/api/campaigns/{campaign['campaign_id']}/send")
    assert r.status_code == 403
    assert "PROSPECT_ALLOW_LIVE_SEND" in r.json()["detail"]


def test_suppressed_email_is_never_queued(client):
    campaign = _approved_campaign(client, count=3)
    emails = client.get(f"/api/campaigns/{campaign['campaign_id']}/emails").json()["items"]
    suppressed_lead_id = emails[0]["lead_id"]
    to_email = emails[0]["to_email"]

    client.post("/api/settings/suppressions", json={"email": to_email, "reason": "test"})

    r = client.post(f"/api/campaigns/{campaign['campaign_id']}/send")
    final = _wait(client, r.json()["job_id"])
    assert final["status"] == "completed"

    # The suppressed lead was skipped during queueing -- it must never
    # reach SENT (or even QUEUED), regardless of how many other approved
    # leads in the same campaign were sent successfully.
    prospect = client.get(f"/api/prospects/{suppressed_lead_id}").json()
    assert prospect["pipeline_status"] not in ("QUEUED", "SENDING", "SENT")


def test_pause_resume_stop_campaign(client):
    campaign = _approved_campaign(client, count=1)
    cid = campaign["campaign_id"]

    r = client.post(f"/api/campaigns/{cid}/pause")
    assert r.status_code == 200
    assert r.json()["run_state"] == "PAUSED"

    r2 = client.post(f"/api/campaigns/{cid}/resume")
    assert r2.json()["run_state"] == "RUNNING"

    r3 = client.post(f"/api/campaigns/{cid}/stop")
    assert r3.json()["run_state"] == "STOPPED"
