from __future__ import annotations

import time

from tests.helpers import seed_qualified_leads


def _create_campaign(client):
    return client.post(
        "/api/campaigns",
        json={"campaign_name": "Job Test", "target_leads": 10},
    ).json()


def _wait(client, job_id, timeout=10):
    deadline = time.time() + timeout
    body = None
    while time.time() < deadline:
        body = client.get(f"/api/jobs/{job_id}").json()
        if body["status"] in ("completed", "failed", "cancelled"):
            return body
        time.sleep(0.02)
    return body


def test_find_emails_job_runs_and_completes(client):
    campaign = _create_campaign(client)
    seed_qualified_leads(campaign["campaign_id"], count=3)

    r = client.post(f"/api/campaigns/{campaign['campaign_id']}/find-emails")
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    final = _wait(client, job_id)
    assert final["status"] == "completed"
    assert final["type"] == "email_discovery"
    assert final["total"] == 3
    assert final["processed"] == 3


def test_job_404(client):
    r = client.get("/api/jobs/does-not-exist")
    assert r.status_code == 404
    r2 = client.post("/api/jobs/does-not-exist/pause")
    assert r2.status_code == 404


def test_jobs_list_filters_by_campaign(client):
    c1 = _create_campaign(client)
    c2 = client.post("/api/campaigns", json={"campaign_name": "Other", "target_leads": 5}).json()
    seed_qualified_leads(c1["campaign_id"], count=1)
    seed_qualified_leads(c2["campaign_id"], count=1)

    j1 = client.post(f"/api/campaigns/{c1['campaign_id']}/find-emails").json()
    j2 = client.post(f"/api/campaigns/{c2['campaign_id']}/find-emails").json()
    _wait(client, j1["job_id"])
    _wait(client, j2["job_id"])

    r = client.get(f"/api/jobs?campaign_id={c1['campaign_id']}")
    ids = [j["id"] for j in r.json()]
    assert j1["job_id"] in ids
    assert j2["job_id"] not in ids


def test_discover_404_for_unknown_campaign(client):
    r = client.post("/api/campaigns/does-not-exist/discover")
    assert r.status_code == 404
