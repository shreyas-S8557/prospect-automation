from __future__ import annotations


def _create(client, **overrides):
    payload = {
        "campaign_name": "Test Campaign",
        "target_titles": ["Founder"],
        "industries": ["SaaS"],
        "locations": ["United States"],
        "target_leads": 10,
    }
    payload.update(overrides)
    return client.post("/api/campaigns", json=payload)


def test_create_campaign(client):
    r = _create(client)
    assert r.status_code == 201
    body = r.json()
    assert body["campaign_name"] == "Test Campaign"
    assert body["target_titles"] == ["Founder"]
    assert body["status"] == "active"
    assert body["run_state"] == "RUNNING"


def test_create_campaign_requires_name(client):
    r = _create(client, campaign_name="")
    assert r.status_code == 422


def test_create_campaign_rejects_bad_age_range(client):
    r = _create(client, age_min=40, age_max=20)
    assert r.status_code == 400


def test_create_campaign_rejects_age_under_18(client):
    r_min = _create(client, age_min=17)
    assert r_min.status_code == 422

    r_max = _create(client, age_max=10)
    assert r_max.status_code == 422


def test_create_campaign_accepts_age_min_only(client):
    r = _create(client, age_min=25)
    assert r.status_code == 201
    body = r.json()
    assert body["age_min"] == 25
    assert body["age_max"] is None


def test_create_campaign_accepts_age_max_only(client):
    r = _create(client, age_max=45)
    assert r.status_code == 201
    body = r.json()
    assert body["age_min"] is None
    assert body["age_max"] == 45


def test_create_campaign_accepts_no_age_filter(client):
    r = _create(client)
    assert r.status_code == 201
    body = r.json()
    assert body["age_min"] is None
    assert body["age_max"] is None


def test_create_campaign_persists_age_range_across_reload(client):
    r = _create(client, age_min=25, age_max=45)
    assert r.status_code == 201
    campaign_id = r.json()["campaign_id"]

    # Simulate reopening the campaign later: a fresh GET must return the
    # exact same age bounds that were stored -- this is what "survive page
    # refresh/reopening" means at the API layer the frontend relies on.
    reload = client.get(f"/api/campaigns/{campaign_id}")
    assert reload.status_code == 200
    body = reload.json()
    assert body["age_min"] == 25
    assert body["age_max"] == 45


def test_discovery_job_receives_persisted_age_range(client):
    """End-to-end proof that the age filter actually reaches the discovery
    pipeline: create a campaign via the real API (same path the frontend
    uses), then reconstruct the TargetConfig the discovery job would
    receive (app.services.campaign_service.get_target_config -- the exact
    function discovery_service._run_discovery_job calls) and confirm the
    age bounds round-tripped intact through the database."""
    from app.services import campaign_service

    r = _create(client, age_min=25, age_max=45)
    assert r.status_code == 201
    campaign_id = r.json()["campaign_id"]

    target = campaign_service.get_target_config(campaign_id)
    assert target.age_min == 25
    assert target.age_max == 45


def test_create_campaign_rejects_unsupported_template_variable(client):
    r = _create(client, email_subject_template="{{not_a_real_variable}}", email_body_template="hi")
    assert r.status_code == 422


def test_get_campaign(client):
    created = _create(client).json()
    r = client.get(f"/api/campaigns/{created['campaign_id']}")
    assert r.status_code == 200
    assert r.json()["campaign_id"] == created["campaign_id"]


def test_get_campaign_404(client):
    r = client.get("/api/campaigns/does-not-exist")
    assert r.status_code == 404


def test_list_campaigns_pagination(client):
    for i in range(3):
        _create(client, campaign_name=f"Campaign {i}")
    r = client.get("/api/campaigns?page=1&page_size=2")
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2


def test_update_campaign(client):
    created = _create(client).json()
    r = client.patch(
        f"/api/campaigns/{created['campaign_id']}",
        json={"campaign_name": "Renamed", "target_titles": ["CEO", "CTO"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["campaign_name"] == "Renamed"
    assert body["target_titles"] == ["CEO", "CTO"]


def test_update_campaign_404(client):
    r = client.patch("/api/campaigns/does-not-exist", json={"campaign_name": "X"})
    assert r.status_code == 404


def test_delete_campaign_archives_not_hard_deletes(client):
    created = _create(client).json()
    r = client.delete(f"/api/campaigns/{created['campaign_id']}")
    assert r.status_code == 204
    # Still readable afterwards (archived, not gone) -- leads/emails are
    # never orphaned by a delete.
    r2 = client.get(f"/api/campaigns/{created['campaign_id']}")
    assert r2.status_code == 200
    assert r2.json()["status"] == "archived"


def test_create_campaign_persists_pain_points(client):
    r = _create(client, pain_points=[
        {"industries": ["Beauty and Wellness"], "label": "no-shows",
         "phrase": "last-minute cancellations and no-shows eating into revenue"},
    ])
    assert r.status_code == 201
    body = r.json()
    assert body["pain_points"] == [
        {"industries": ["Beauty and Wellness"], "label": "no-shows",
         "phrase": "last-minute cancellations and no-shows eating into revenue"},
    ]

    reload = client.get(f"/api/campaigns/{body['campaign_id']}")
    assert reload.json()["pain_points"] == body["pain_points"]


def test_create_campaign_no_pain_points_defaults_empty(client):
    r = _create(client)
    assert r.status_code == 201
    assert r.json()["pain_points"] == []
