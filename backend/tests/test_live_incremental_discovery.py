"""
Simulates the exact scenario from the live bug report: a slow discovery run
(DDGS + local LLM) that finds candidates one at a time over real wall-clock
time. `pipeline.orchestrator.run_discovery` is monkeypatched to behave like
the real one (calling on_candidate/on_query_progress incrementally, with
real `time.sleep()` between finds) but without touching the network or a
local LLM -- this isolates and proves the *backend persistence contract*
fix (the actual root cause) without depending on external services this
sandbox cannot reach.

This test would FAIL against the original discovery_service.py, because
that version never called any callback and only wrote to the DB after the
(here-simulated) full run returned -- i.e. polling mid-run would have seen
0 discovered/0 processed/empty prospects the whole time, exactly as
reported live.
"""
from __future__ import annotations

import time

import pytest


def _create_campaign(client):
    return client.post(
        "/api/campaigns",
        json={
            "campaign_name": "Saas founders",
            "target_leads": 5,
            "titles": ["Founder", "CTO", "CEO"],
            "industries": ["SaaS"],
            "locations": ["United States"],
            "keywords": ["AI", "automation"],
        },
    ).json()


def _fake_run_discovery(target, *, phases=None, on_candidate=None, on_query_progress=None, **kw):
    """Stand-in for orchestrator.run_discovery: mimics real timing (a
    fraction of a second per search query, occasional accepted candidate)
    and calls the exact same callbacks the real ddgs phase calls."""
    import csv
    import tempfile
    from pathlib import Path

    rows = []
    total_queries = 40
    for i in range(1, total_queries + 1):
        if on_query_progress:
            on_query_progress(i, total_queries)
        time.sleep(0.01)  # stand-in for a real search request round-trip
        if i % 8 == 0 and len(rows) < target.target_count:
            row = {
                "name": f"Founder {i}",
                "location": "United States",
                "linkedin_url": f"https://www.linkedin.com/in/founder-{i}",
                "profile_title": "Founder & CEO | AI SaaS",
                "summary": "AI automation SaaS founder based in the United States.",
                "industries": "SaaS",
                "email": "",
                "phone": "",
                "source": "ddgs_search",
                "company_name": f"Company{i}",
                "age": "",
                "age_source": "",
                "age_confidence": "none",
                "qualification_status": "",
            }
            rows.append(row)
            if on_candidate:
                on_candidate(row)
        if len(rows) >= target.target_count:
            break

    tmp = Path(tempfile.mkdtemp()) / "out.csv"
    fieldnames = list(rows[0].keys()) if rows else ["name"]
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return tmp


def test_prospects_and_stats_update_incrementally_during_discovery(client, monkeypatch):
    from app.services import discovery_service

    monkeypatch.setattr(discovery_service.orchestrator, "run_discovery", _fake_run_discovery)

    campaign = _create_campaign(client)
    cid = campaign["campaign_id"]

    r = client.post(f"/api/campaigns/{cid}/discover")
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    # Poll mid-run, the same way the frontend's pollJob() does, and assert
    # we observe the DB and job counters moving *before* the job finishes --
    # this is the exact behavior that was broken live.
    saw_partial_prospects = False
    saw_partial_processed = False
    saw_query_progress = False
    deadline = time.time() + 5
    while time.time() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        prospects = client.get(f"/api/campaigns/{cid}/prospects?page_size=100").json()

        if job["status"] == "running":
            if 0 < job["processed"] < job["total"]:
                saw_partial_processed = True
            if job.get("stats", {}).get("queries_done", 0) > 0:
                saw_query_progress = True
            if 0 < len(prospects["items"]) < campaign.get("target_leads", 5):
                saw_partial_prospects = True

        if job["status"] in ("completed", "failed", "cancelled"):
            break
        time.sleep(0.02)

    final_job = client.get(f"/api/jobs/{job_id}").json()
    assert final_job["status"] == "completed"

    final_prospects = client.get(f"/api/campaigns/{cid}/prospects?page_size=100").json()
    final_stats = client.get(f"/api/campaigns/{cid}/stats").json()

    # The core regression check: mid-run polling actually saw non-zero,
    # non-final numbers -- i.e. data was visible *during* the run, not only
    # after it finished.
    assert saw_partial_processed, "job.processed never showed a partial (mid-run) value"
    assert saw_partial_prospects, "prospects endpoint never showed partial results mid-run"
    assert saw_query_progress, "job.stats.queries_done was never reported mid-run"

    # And the final state is fully consistent end to end.
    assert len(final_prospects["items"]) == 5
    assert final_stats["discovered"] >= 5
