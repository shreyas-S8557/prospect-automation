"""Day 4 milestone tests: canonical Lead model + pipeline state machine.

Run with (from the `scripts/` directory, or as a module from repo root):
    python -m unittest pipeline.test_lead_pipeline_day4 -v

Does NOT hit the network, an LLM, or any external service — everything here
is pure Python + stdlib sqlite3 (in-memory), matching the constraint that
Day 4 is model + state-machine + persistence only.

Covers (per the Day 4 spec):
  - Lead creation
  - Lead normalization (InvestorRow -> Lead)
  - LinkedIn URL deduplication
  - name + company fallback deduplication
  - state transitions (valid)
  - invalid state transitions (including DISCOVERED -> SENT skip-stage guard)
  - persistence / reloading
  - resuming an interrupted pipeline
  - Day 2 regression (TargetConfig)
  - Day 3 regression (matches_target_criteria)
"""

from __future__ import annotations

import unittest
from pathlib import Path

from .lead_pipeline import (
    compute_identity_key,
    ingest_discovery_rows,
    normalize_investor_row,
    qualify_pending_leads,
    split_name,
)
from .lead_store import LeadStore
from .models import (
    ALLOWED_TRANSITIONS,
    InvalidStateTransition,
    Lead,
    PipelineStatus,
    TERMINAL_STATUSES,
    validate_transition,
)
from .quality import matches_target_criteria
from .query_generator import build_queries
from .target_config import CPA_PARTNER_PRESET, TargetConfig


def make_row(**overrides):
    row = {
        "name": "Jane Doe",
        "location": "San Francisco, California, United States",
        "linkedin_url": "https://www.linkedin.com/in/janedoe/",
        "profile_title": "Founder & CEO",
        "summary": "Building a SaaS company for accountants.",
        "industries": "SaaS",
        "email": "",
        "phone": "",
        "source": "ddgs_search",
        "company_name": "Acme Corp",
        "company_size": "25",
        "age": "",
        "age_source": "",
        "age_confidence": "",
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# 1. Lead creation
# ---------------------------------------------------------------------------


class TestLeadCreation(unittest.TestCase):
    def test_default_lead_has_generated_id_and_discovered_status(self):
        lead = Lead(first_name="Jane", last_name="Doe")
        self.assertTrue(lead.lead_id)
        self.assertEqual(lead.pipeline_status, PipelineStatus.DISCOVERED.value)
        self.assertEqual(lead.full_name, "Jane Doe")

    def test_two_leads_get_distinct_ids(self):
        a, b = Lead(), Lead()
        self.assertNotEqual(a.lead_id, b.lead_id)

    def test_accepts_pipeline_status_as_enum_or_string(self):
        a = Lead(pipeline_status=PipelineStatus.QUALIFIED)
        b = Lead(pipeline_status="QUALIFIED")
        self.assertEqual(a.pipeline_status, b.pipeline_status)
        self.assertEqual(a.status, PipelineStatus.QUALIFIED)

    def test_rejects_unknown_pipeline_status(self):
        with self.assertRaises(ValueError):
            Lead(pipeline_status="NOT_A_REAL_STATUS")

    def test_round_trips_through_to_dict_from_dict(self):
        lead = Lead(first_name="A", last_name="B", company_name="Acme")
        again = Lead.from_dict(lead.to_dict())
        self.assertEqual(lead, again)


# ---------------------------------------------------------------------------
# 2. Lead normalization (InvestorRow -> Lead)
# ---------------------------------------------------------------------------


class TestLeadNormalization(unittest.TestCase):
    def test_name_splitting(self):
        self.assertEqual(split_name("Jane Doe"), ("Jane", "Doe"))
        self.assertEqual(split_name("Jane Q. Doe"), ("Jane", "Q. Doe"))
        self.assertEqual(split_name("Madonna"), ("Madonna", ""))
        self.assertEqual(split_name("  "), ("", ""))

    def test_normalizes_investor_row_into_discovered_lead(self):
        lead = normalize_investor_row(make_row(), campaign_id="camp-1")
        self.assertEqual(lead.first_name, "Jane")
        self.assertEqual(lead.last_name, "Doe")
        self.assertEqual(lead.job_title, "Founder & CEO")
        self.assertEqual(lead.company_name, "Acme Corp")
        self.assertEqual(lead.linkedin_url, "https://www.linkedin.com/in/janedoe")
        self.assertEqual(lead.campaign_id, "camp-1")
        self.assertEqual(lead.pipeline_status, PipelineStatus.DISCOVERED.value)
        self.assertEqual(lead.discovery_source, "ddgs_search")

    def test_normalization_never_invents_email_or_age(self):
        lead = normalize_investor_row(make_row(email="", age=""), campaign_id="c")
        self.assertEqual(lead.email, "")
        self.assertEqual(lead.age, "")
        self.assertEqual(lead.email_status, "unknown")


# ---------------------------------------------------------------------------
# 3. Deduplication
# ---------------------------------------------------------------------------


class TestDeduplication(unittest.TestCase):
    def test_linkedin_url_is_primary_identity(self):
        key_a = compute_identity_key(linkedin_url="https://linkedin.com/in/janedoe/")
        key_b = compute_identity_key(linkedin_url="linkedin.com/in/JaneDoe")
        self.assertEqual(key_a, key_b)
        self.assertTrue(key_a.startswith("li:"))

    def test_name_plus_company_fallback_when_no_linkedin(self):
        key_a = compute_identity_key(full_name="Jane Doe", company_name="Acme Corp")
        key_b = compute_identity_key(full_name="jane   doe", company_name="ACME, Corp.")
        self.assertEqual(key_a, key_b)
        self.assertTrue(key_a.startswith("nc:"))

    def test_no_identity_when_nothing_usable(self):
        self.assertEqual(compute_identity_key(), "")
        self.assertEqual(compute_identity_key(full_name="Jane Doe"), "")  # no company

    def test_same_person_two_queries_dedupes_via_linkedin(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        row1 = make_row(source="ddgs_search")
        row2 = make_row(source="exa_people", summary="Longer bio from a different query.")
        stats = ingest_discovery_rows(store, [row1, row2], campaign_id="c1")
        self.assertEqual(stats["created"], 1)
        self.assertEqual(stats["updated"], 1)
        leads = store.all(campaign_id="c1")
        self.assertEqual(len(leads), 1)

    def test_same_person_dedupes_via_name_and_company_when_no_linkedin(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        row1 = make_row(linkedin_url="", name="Jane Doe", company_name="Acme Corp")
        row2 = make_row(linkedin_url="", name="jane doe", company_name="Acme, Corp.")
        stats = ingest_discovery_rows(store, [row1, row2], campaign_id="c1")
        self.assertEqual(stats["created"], 1)
        self.assertEqual(stats["updated"], 1)
        self.assertEqual(len(store.all(campaign_id="c1")), 1)

    def test_different_people_are_not_merged(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        row1 = make_row(name="Jane Doe", linkedin_url="https://linkedin.com/in/janedoe")
        row2 = make_row(name="John Smith", linkedin_url="https://linkedin.com/in/johnsmith")
        ingest_discovery_rows(store, [row1, row2], campaign_id="c1")
        self.assertEqual(len(store.all(campaign_id="c1")), 2)

    def test_dedup_is_scoped_per_campaign(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        row = make_row()
        ingest_discovery_rows(store, [row], campaign_id="camp-a")
        ingest_discovery_rows(store, [row], campaign_id="camp-b")
        self.assertEqual(len(store.all(campaign_id="camp-a")), 1)
        self.assertEqual(len(store.all(campaign_id="camp-b")), 1)


# ---------------------------------------------------------------------------
# 4. State transitions
# ---------------------------------------------------------------------------


class TestStateTransitions(unittest.TestCase):
    def test_discovered_to_qualified_is_legal(self):
        result = validate_transition(PipelineStatus.DISCOVERED, PipelineStatus.QUALIFIED)
        self.assertEqual(result, PipelineStatus.QUALIFIED)

    def test_full_happy_path_is_legal(self):
        path = [
            PipelineStatus.DISCOVERED,
            PipelineStatus.QUALIFIED,
            PipelineStatus.EMAIL_CANDIDATES_FOUND,
            PipelineStatus.EMAIL_VALIDATED,
            PipelineStatus.EMAIL_GENERATED,
            PipelineStatus.APPROVED,
            PipelineStatus.QUEUED,
            PipelineStatus.SENDING,
            PipelineStatus.SENT,
        ]
        for current, nxt in zip(path, path[1:]):
            self.assertEqual(validate_transition(current, nxt), nxt)

    def test_terminal_states_have_no_outgoing_transitions(self):
        for status in TERMINAL_STATUSES:
            self.assertEqual(ALLOWED_TRANSITIONS[status], frozenset())

    def test_store_transition_updates_status_and_timestamp(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = Lead()
        store.upsert_lead(lead)
        before = store.get(lead.lead_id)
        updated = store.transition(lead.lead_id, PipelineStatus.QUALIFIED)
        self.assertEqual(updated.pipeline_status, PipelineStatus.QUALIFIED.value)
        self.assertGreaterEqual(updated.updated_at, before.updated_at)


# ---------------------------------------------------------------------------
# 5. Invalid state transitions
# ---------------------------------------------------------------------------


class TestInvalidStateTransitions(unittest.TestCase):
    def test_cannot_skip_stages_discovered_to_sent(self):
        with self.assertRaises(InvalidStateTransition):
            validate_transition(PipelineStatus.DISCOVERED, PipelineStatus.SENT)

    def test_cannot_move_backwards(self):
        with self.assertRaises(InvalidStateTransition):
            validate_transition(PipelineStatus.QUALIFIED, PipelineStatus.DISCOVERED)

    def test_cannot_transition_out_of_terminal_state(self):
        with self.assertRaises(InvalidStateTransition):
            validate_transition(PipelineStatus.FILTERED_OUT, PipelineStatus.QUALIFIED)
        with self.assertRaises(InvalidStateTransition):
            validate_transition(PipelineStatus.SENT, PipelineStatus.QUEUED)

    def test_cannot_self_transition(self):
        with self.assertRaises(InvalidStateTransition):
            validate_transition(PipelineStatus.QUALIFIED, PipelineStatus.QUALIFIED)

    def test_store_transition_raises_and_does_not_mutate_on_illegal_move(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = Lead()
        store.upsert_lead(lead)
        with self.assertRaises(InvalidStateTransition):
            store.transition(lead.lead_id, PipelineStatus.SENT)
        # Status must be unchanged after the failed attempt.
        self.assertEqual(store.get(lead.lead_id).pipeline_status, PipelineStatus.DISCOVERED.value)

    def test_store_transition_unknown_lead_raises_keyerror(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        with self.assertRaises(KeyError):
            store.transition("does-not-exist", PipelineStatus.QUALIFIED)


# ---------------------------------------------------------------------------
# 6. Persistence / reloading
# ---------------------------------------------------------------------------


class TestPersistence(unittest.TestCase):
    def test_reload_from_same_sqlite_file(self):
        db_path = Path("/tmp/day4_lead_store_test.db")
        db_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        try:
            store1 = LeadStore(db_path)
            self.addCleanup(store1.close)
            lead = normalize_investor_row(make_row(), campaign_id="c1")
            store1.upsert_lead(lead)
            store1.transition(lead.lead_id, PipelineStatus.QUALIFIED)
            store1.close()

            store2 = LeadStore(db_path)
            self.addCleanup(store2.close)
            reloaded = store2.get(lead.lead_id)
            self.assertIsNotNone(reloaded)
            self.assertEqual(reloaded.pipeline_status, PipelineStatus.QUALIFIED.value)
            self.assertEqual(reloaded.linkedin_url, lead.linkedin_url)
            store2.close()
        finally:
            db_path.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(db_path) + suffix).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# 7. Resuming an interrupted pipeline
# ---------------------------------------------------------------------------


class TestResumability(unittest.TestCase):
    def test_qualify_pending_only_touches_discovered_leads(self):
        target = TargetConfig(name="saas", titles=["Founder", "CEO"], industries=["SaaS"])
        store = LeadStore(":memory:")
        self.addCleanup(store.close)

        matching_rows = [make_row(name=f"Match {i}", linkedin_url=f"https://linkedin.com/in/match{i}") for i in range(3)]
        non_matching_rows = [
            make_row(
                name=f"NoMatch {i}",
                linkedin_url=f"https://linkedin.com/in/nomatch{i}",
                profile_title="Managing Partner",
                summary="CPA firm",
                industries="accounting",
            )
            for i in range(2)
        ]
        ingest_discovery_rows(store, matching_rows + non_matching_rows, campaign_id="c1")

        # Simulate "interruption": qualify once (first pass, e.g. process
        # crashes/stops right after this).
        stats1 = qualify_pending_leads(store, campaign_id="c1", target=target)
        self.assertEqual(stats1["qualified"], 3)
        self.assertEqual(stats1["filtered_out"], 2)
        self.assertEqual(len(store.list_by_status(PipelineStatus.DISCOVERED, campaign_id="c1")), 0)

        # "Resume": calling qualify_pending_leads again must be a no-op —
        # nothing left in DISCOVERED, so nothing is reprocessed.
        stats2 = qualify_pending_leads(store, campaign_id="c1", target=target)
        self.assertEqual(stats2, {"qualified": 0, "filtered_out": 0})

    def test_resumed_discovery_does_not_reset_progressed_lead(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        row = make_row()
        ingest_discovery_rows(store, [row], campaign_id="c1")
        [lead] = store.all(campaign_id="c1")
        store.transition(lead.lead_id, PipelineStatus.QUALIFIED)
        store.transition(lead.lead_id, PipelineStatus.EMAIL_CANDIDATES_FOUND)

        # Re-running discovery (e.g. after a restart) re-ingests the same row.
        ingest_discovery_rows(store, [row], campaign_id="c1")

        [still_one_lead] = store.all(campaign_id="c1")
        self.assertEqual(still_one_lead.lead_id, lead.lead_id)
        self.assertEqual(still_one_lead.pipeline_status, PipelineStatus.EMAIL_CANDIDATES_FOUND.value)

    def test_count_by_status_gives_a_resume_snapshot(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        rows = [make_row(name=f"P{i}", linkedin_url=f"https://linkedin.com/in/p{i}") for i in range(5)]
        ingest_discovery_rows(store, rows, campaign_id="c1")
        for lead in store.list_by_status(PipelineStatus.DISCOVERED, campaign_id="c1")[:3]:
            store.transition(lead.lead_id, PipelineStatus.QUALIFIED)
        counts = store.count_by_status(campaign_id="c1")
        self.assertEqual(counts.get("QUALIFIED"), 3)
        self.assertEqual(counts.get("DISCOVERED"), 2)


# ---------------------------------------------------------------------------
# 8. Day 2 regression (TargetConfig)
# ---------------------------------------------------------------------------


class TestDay2Regression(unittest.TestCase):
    def test_target_config_from_dict_and_query_generation_still_works(self):
        cfg = TargetConfig.from_dict(
            {"name": "saas_founders", "titles": ["Founder", "CEO"], "industries": ["SaaS"], "target_count": 100}
        )
        queries = build_queries(cfg)
        self.assertGreater(len(queries), 0)
        self.assertEqual(cfg.output_stem(), "saas_founders")

    def test_cpa_preset_unchanged(self):
        self.assertEqual(CPA_PARTNER_PRESET.name, "us_cpa_partners")
        self.assertIn("managing partner", CPA_PARTNER_PRESET.titles)
        self.assertGreater(len(build_queries(CPA_PARTNER_PRESET)), 0)


# ---------------------------------------------------------------------------
# 9. Day 3 regression (matches_target_criteria)
# ---------------------------------------------------------------------------


class TestDay3Regression(unittest.TestCase):
    def test_matches_target_criteria_still_qualifies_and_rejects(self):
        target = TargetConfig(name="saas", titles=["Founder", "CEO"], industries=["SaaS"])
        good = make_row()
        bad = make_row(profile_title="Managing Partner", summary="CPA firm", industries="accounting")
        self.assertTrue(matches_target_criteria(good, target))
        self.assertFalse(matches_target_criteria(bad, target))

    def test_cpa_preset_still_qualifies_cpa_rows(self):
        cpa_row = make_row(profile_title="Managing Partner, CPA", summary="Managing partner at a CPA firm", industries="accounting")
        self.assertTrue(matches_target_criteria(cpa_row, CPA_PARTNER_PRESET))


if __name__ == "__main__":
    unittest.main()
