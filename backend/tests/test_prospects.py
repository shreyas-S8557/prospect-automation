from __future__ import annotations

from tests.helpers import seed_qualified_leads


def _create_campaign(client):
    return client.post(
        "/api/campaigns",
        json={"campaign_name": "Prospect Test", "target_leads": 10},
    ).json()


def test_prospect_list_and_pagination(client):
    campaign = _create_campaign(client)
    seed_qualified_leads(campaign["campaign_id"], count=5)

    r = client.get(f"/api/campaigns/{campaign['campaign_id']}/prospects?page=1&page_size=2")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2


def test_prospect_search_filter(client):
    campaign = _create_campaign(client)
    seed_qualified_leads(campaign["campaign_id"], count=3)

    r = client.get(f"/api/campaigns/{campaign['campaign_id']}/prospects?search=Acme1")
    body = r.json()
    assert body["total"] == 1
    assert "Acme1" in body["items"][0]["company_name"]


def test_prospect_status_filter(client):
    campaign = _create_campaign(client)
    seed_qualified_leads(campaign["campaign_id"], count=3)

    r = client.get(f"/api/campaigns/{campaign['campaign_id']}/prospects?pipeline_status=QUALIFIED")
    assert r.json()["total"] == 3

    r2 = client.get(f"/api/campaigns/{campaign['campaign_id']}/prospects?pipeline_status=SENT")
    assert r2.json()["total"] == 0


def test_prospect_detail_404(client):
    r = client.get("/api/prospects/does-not-exist")
    assert r.status_code == 404


def test_prospect_detail_and_update(client):
    campaign = _create_campaign(client)
    leads = seed_qualified_leads(campaign["campaign_id"], count=1)
    lead_id = leads[0].lead_id

    r = client.get(f"/api/prospects/{lead_id}")
    assert r.status_code == 200
    assert r.json()["qualification_status"] == "qualified"

    r2 = client.patch(f"/api/prospects/{lead_id}", json={"job_title": "New Title"})
    assert r2.status_code == 200
    assert r2.json()["job_title"] == "New Title"


def test_prospects_for_unknown_campaign_404(client):
    r = client.get("/api/campaigns/does-not-exist/prospects")
    assert r.status_code == 404
