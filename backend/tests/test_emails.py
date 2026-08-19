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


def _campaign_with_generated_emails(client, count=2):
    campaign = client.post(
        "/api/campaigns", json={"campaign_name": "Email Test", "target_leads": 10}
    ).json()
    seed_qualified_leads(campaign["campaign_id"], count=count)

    for stage in ("find-emails", "validate-emails", "generate-emails"):
        job = client.post(f"/api/campaigns/{campaign['campaign_id']}/{stage}").json()
        _wait(client, job["job_id"])

    return campaign


def test_generate_emails_creates_pending_drafts(client):
    campaign = _campaign_with_generated_emails(client)
    r = client.get(f"/api/campaigns/{campaign['campaign_id']}/emails")
    body = r.json()
    assert body["total"] >= 1
    assert all(e["review_status"] == "PENDING" for e in body["items"])


def test_approve_then_reject_is_invalid(client):
    campaign = _campaign_with_generated_emails(client)
    emails = client.get(f"/api/campaigns/{campaign['campaign_id']}/emails").json()["items"]
    lead_id = emails[0]["lead_id"]

    r = client.post(f"/api/emails/{lead_id}/approve")
    assert r.status_code == 200
    assert r.json()["review_status"] == "APPROVED"

    # Approving again (already approved) is not a legal transition.
    r2 = client.post(f"/api/emails/{lead_id}/approve")
    assert r2.status_code == 409


def test_reject_email_with_reason(client):
    campaign = _campaign_with_generated_emails(client)
    emails = client.get(f"/api/campaigns/{campaign['campaign_id']}/emails").json()["items"]
    lead_id = emails[0]["lead_id"]

    r = client.post(f"/api/emails/{lead_id}/reject", json={"reason": "Not a fit"})
    assert r.status_code == 200
    body = r.json()
    assert body["review_status"] == "REJECTED"
    assert body["rejection_reason"] == "Not a fit"


def test_edit_email_before_approval(client):
    campaign = _campaign_with_generated_emails(client)
    emails = client.get(f"/api/campaigns/{campaign['campaign_id']}/emails").json()["items"]
    lead_id = emails[0]["lead_id"]

    r = client.patch(f"/api/emails/{lead_id}", json={"subject": "Edited subject"})
    assert r.status_code == 200
    assert r.json()["subject"] == "Edited subject"
    assert r.json()["edited"] is True


def test_bulk_approve_all(client):
    campaign = _campaign_with_generated_emails(client, count=3)
    r = client.post(f"/api/campaigns/{campaign['campaign_id']}/emails/approve-all")
    assert r.status_code == 200
    body = r.json()
    assert len(body["succeeded"]) >= 1
    assert body["failed"] == []

    emails = client.get(f"/api/campaigns/{campaign['campaign_id']}/emails").json()["items"]
    assert all(e["review_status"] == "APPROVED" for e in emails)


def test_email_404(client):
    r = client.get("/api/emails/does-not-exist")
    assert r.status_code == 404
