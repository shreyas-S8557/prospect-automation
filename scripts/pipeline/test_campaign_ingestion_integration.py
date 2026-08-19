"""Regression tests for the missing Campaign-lifecycle integration bug:

    ingest_csv_to_leadstore.py created/updated Leads under a campaign_id
    but never created a matching Campaign row, so every downstream stage
    that does load_campaign(store, campaign_id) -- email_generation.py,
    email_sending.py, campaign_stats.py, campaign_control.py -- failed with
    "No campaign with campaign_id=...".

Fix under test:
  - pipeline/campaign.py gained ensure_campaign(): idempotent
    get-or-create for a Campaign row, never destructively overwriting one
    that already exists.
  - pipeline/target_config.py gained optional email_subject_template /
    email_body_template / email_sender_name / campaign_name /
    campaign_description fields, so campaign copy can be config-driven
    without hard-coding anything vertical-specific into generic code.
  - scripts/ingest_csv_to_leadstore.py now calls ensure_campaign() so a
    Campaign row always exists for --campaign-id after ingestion.

Run with (from the `scripts/` directory):
    python -m unittest pipeline.test_campaign_ingestion_integration -v

No network, no LLM -- pure SQLite (:memory: for unit tests, a real temp
file for the CLI-level integration tests) and the CSV fixture below is
synthetic. No email is ever sent by this file.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))

from pipeline.campaign import (  # noqa: E402
    DEFAULT_BODY_TEMPLATE,
    DEFAULT_SUBJECT_TEMPLATE,
    UnsupportedTemplateVariable,
    create_campaign,
    ensure_campaign,
    load_campaign,
    save_campaign,
    validate_template,
)
from pipeline.email_generation import generate_pending_emails  # noqa: E402
from pipeline.lead_store import LeadStore  # noqa: E402
from pipeline.models import PipelineStatus  # noqa: E402
from pipeline.target_config import TargetConfig  # noqa: E402


def _load_ingest_module():
    """Import scripts/ingest_csv_to_leadstore.py as a module.

    It's a top-level script (not part of the `pipeline` package), so it's
    loaded by file path rather than a normal import -- the same approach
    its own `if __name__ == "__main__"` entry point relies on implicitly
    when run as `python3 scripts/ingest_csv_to_leadstore.py`.
    """
    path = SCRIPTS_DIR / "ingest_csv_to_leadstore.py"
    spec = importlib.util.spec_from_file_location("ingest_csv_to_leadstore", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


INGEST = _load_ingest_module()

SYNTHETIC_CSV_ROWS = [
    {
        "name": "Test Persona One",
        "location": "Test City, CA, United States",
        "linkedin_url": "https://linkedin.com/in/test-persona-one",
        "profile_title": "Founder and CEO",
        "summary": "Synthetic fixture for campaign-integration regression tests.",
        "industries": "SaaS; AI",
        "email": "",
        "phone": "",
        "source": "synthetic_test",
        "company_name": "Example Test SaaS Co",
        "company_size": "",
        "age": "27",
        "age_source": "synthetic",
        "age_confidence": "high",
        "qualification_status": "qualified",
        "qualification_reason": "synthetic fixture",
        "keyword_relevance": "strong",
        "keyword_evidence": "SaaS; AI",
        "industry_evidence": "SaaS; AI",
    },
    {
        "name": "Test Persona Two",
        "location": "Test City, CA, United States",
        "linkedin_url": "https://linkedin.com/in/test-persona-two",
        "profile_title": "Co-Founder",
        "summary": "Synthetic fixture for campaign-integration regression tests.",
        "industries": "SaaS; Automation",
        "email": "",
        "phone": "",
        "source": "synthetic_test",
        "company_name": "Another Test SaaS Co",
        "company_size": "",
        "age": "29",
        "age_source": "synthetic",
        "age_confidence": "high",
        "qualification_status": "qualified",
        "qualification_reason": "synthetic fixture",
        "keyword_relevance": "strong",
        "keyword_evidence": "SaaS; Automation",
        "industry_evidence": "SaaS; Automation",
    },
]


def _write_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = list(rows[0].keys())
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _write_target_config(path: Path, **overrides) -> None:
    cfg = dict(
        locations=["United States"],
        titles=["Founder", "CEO", "Co-Founder"],
        industries=["SaaS"],
        keywords=["AI", "automation"],
        company_size_min=None,
        company_size_max=None,
        age_min=22,
        age_max=35,
        target_count=50,
        exclude_keywords=[],
        name="saas_ai_founders",
    )
    cfg.update(overrides)
    import json

    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. ensure_campaign() unit tests -- the core lifecycle primitive.
# ---------------------------------------------------------------------------


class TestEnsureCampaignUnit(unittest.TestCase):
    def test_creates_campaign_when_missing(self):
        store = LeadStore(":memory:")
        self.assertIsNone(load_campaign(store, "camp_a"))

        campaign = ensure_campaign(store, "camp_a", name="Camp A")

        self.assertEqual(campaign.campaign_id, "camp_a")
        reloaded = load_campaign(store, "camp_a")
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded.campaign_id, "camp_a")
        self.assertEqual(reloaded.name, "Camp A")

    def test_second_call_is_idempotent_no_duplicate(self):
        store = LeadStore(":memory:")
        ensure_campaign(store, "camp_b", name="Camp B")
        ensure_campaign(store, "camp_b", name="Camp B")
        ensure_campaign(store, "camp_b", name="Camp B")

        rows = [row for row in store.list_campaigns() if row["campaign_id"] == "camp_b"]
        self.assertEqual(len(rows), 1)

    def test_does_not_overwrite_existing_custom_campaign(self):
        store = LeadStore(":memory:")
        custom = create_campaign(
            "Hand-authored campaign",
            "My custom subject {{first_name}}",
            "My custom body {{company_name}}",
            campaign_id="camp_c",
        )
        save_campaign(store, custom)

        returned = ensure_campaign(
            store,
            "camp_c",
            name="Should be ignored",
            subject_template="Should also be ignored {{first_name}}",
            body_template="Ignored body {{company_name}}",
        )

        self.assertEqual(returned.name, "Hand-authored campaign")
        self.assertEqual(returned.subject_template, "My custom subject {{first_name}}")
        self.assertEqual(returned.body_template, "My custom body {{company_name}}")

        reloaded = load_campaign(store, "camp_c")
        self.assertEqual(reloaded.name, "Hand-authored campaign")

    def test_falls_back_to_campaign_id_when_no_name_given(self):
        store = LeadStore(":memory:")
        campaign = ensure_campaign(store, "unnamed_campaign")
        self.assertEqual(campaign.name, "unnamed_campaign")

    def test_default_templates_are_generic_and_pass_validation(self):
        # Requirement: no vertical-specific (e.g. SaaS) copy in generic code.
        self.assertNotIn("SaaS", DEFAULT_SUBJECT_TEMPLATE)
        self.assertNotIn("SaaS", DEFAULT_BODY_TEMPLATE)
        validate_template(DEFAULT_SUBJECT_TEMPLATE)
        validate_template(DEFAULT_BODY_TEMPLATE)

    def test_target_config_templates_flow_into_campaign(self):
        store = LeadStore(":memory:")
        target = TargetConfig(
            name="fintech_campaign",
            campaign_name="Fintech Outreach",
            email_subject_template="Hello {{first_name}} from {{company_name}}",
            email_body_template="Hi {{first_name}}, re: {{industry}}.",
            email_sender_name="Alex",
        )
        campaign = ensure_campaign(
            store,
            "camp_d",
            name=target.campaign_name,
            subject_template=target.email_subject_template,
            body_template=target.email_body_template,
            sender_name=target.email_sender_name,
        )
        self.assertEqual(campaign.name, "Fintech Outreach")
        self.assertEqual(campaign.subject_template, "Hello {{first_name}} from {{company_name}}")
        self.assertEqual(campaign.sender_name, "Alex")

    def test_unsupported_variable_in_config_template_raises(self):
        store = LeadStore(":memory:")
        with self.assertRaises(UnsupportedTemplateVariable):
            ensure_campaign(
                store,
                "camp_e",
                subject_template="Hi {{not_a_real_variable}}",
                body_template="Body {{first_name}}",
            )
        # And nothing was persisted.
        self.assertIsNone(load_campaign(store, "camp_e"))


# ---------------------------------------------------------------------------
# 2. ingest_csv_to_leadstore.py CLI-level integration tests.
# ---------------------------------------------------------------------------


class TestIngestScriptCreatesCampaign(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.tmp_path = Path(self.tmpdir.name)

        self.csv_path = self.tmp_path / "leads.csv"
        _write_csv(self.csv_path, SYNTHETIC_CSV_ROWS)

        self.config_path = self.tmp_path / "target.json"
        _write_target_config(self.config_path)

        self.db_path = self.tmp_path / "test.db"

    def _run_ingest(self, campaign_id: str = "saas_ai_founders_test") -> int:
        argv = [
            "ingest_csv_to_leadstore.py",
            "--csv", str(self.csv_path),
            "--campaign-id", campaign_id,
            "--config", str(self.config_path),
            "--db", str(self.db_path),
        ]
        old_argv = sys.argv
        sys.argv = argv
        try:
            return INGEST.main()
        finally:
            sys.argv = old_argv

    def test_ingest_creates_campaign_row(self):
        rc = self._run_ingest("camp_created")
        self.assertEqual(rc, 0)

        with LeadStore(str(self.db_path)) as store:
            campaign = load_campaign(store, "camp_created")
            self.assertIsNotNone(campaign, "ingest_csv_to_leadstore.py must create a Campaign row")
            # Requirement 10: campaign_id must be exactly the supplied --campaign-id.
            self.assertEqual(campaign.campaign_id, "camp_created")
            self.assertTrue(campaign.subject_template.strip())
            self.assertTrue(campaign.body_template.strip())

    def test_reingesting_same_campaign_does_not_duplicate(self):
        self._run_ingest("camp_repeat")
        self._run_ingest("camp_repeat")
        self._run_ingest("camp_repeat")

        with LeadStore(str(self.db_path)) as store:
            rows = [r for r in store.list_campaigns() if r["campaign_id"] == "camp_repeat"]
            self.assertEqual(len(rows), 1, "re-ingestion must not create duplicate Campaign rows")

            # Leads themselves also stay de-duplicated across the repeated runs.
            leads = store.all(campaign_id="camp_repeat")
            self.assertEqual(len(leads), len(SYNTHETIC_CSV_ROWS))

    def test_leads_are_still_ingested_and_qualified(self):
        # Guard against requirement 2 (don't change working email-discovery-
        # feeding behaviour): leads must still land in QUALIFIED exactly as
        # before this fix.
        self._run_ingest("camp_leads_check")
        with LeadStore(str(self.db_path)) as store:
            qualified = store.list_by_status(PipelineStatus.QUALIFIED, campaign_id="camp_leads_check")
            self.assertEqual(len(qualified), len(SYNTHETIC_CSV_ROWS))

    def test_email_generation_can_load_the_campaign(self):
        # Simulates `python -m pipeline.email_generation --campaign-id ...`:
        # the exact failure mode from the bug report was load_campaign()
        # returning None here.
        self._run_ingest("camp_for_email_gen")

        with LeadStore(str(self.db_path)) as store:
            campaign = load_campaign(store, "camp_for_email_gen")
            self.assertIsNotNone(campaign)

            # Prove it's not just loadable but *usable*: move a lead to
            # EMAIL_VALIDATED (the precondition generate_pending_emails
            # expects) and confirm generation actually succeeds against the
            # campaign ingestion created -- exercising the same template
            # validation email_generation.py relies on at runtime.
            leads = store.list_by_status(PipelineStatus.QUALIFIED, campaign_id="camp_for_email_gen")
            self.assertTrue(leads)
            for lead in leads:
                store.transition(lead.lead_id, PipelineStatus.EMAIL_CANDIDATES_FOUND)
                lead = store.transition(lead.lead_id, PipelineStatus.EMAIL_VALIDATED)
                lead.email = f"{(lead.first_name or 'lead').lower()}@example-test.com"
                store.save(lead)

            stats = generate_pending_emails(store, campaign)
            self.assertEqual(stats["failed"], 0)
            self.assertEqual(stats["generated"], len(leads))

    def test_does_not_overwrite_a_preexisting_hand_authored_campaign(self):
        # Requirement 9: existing campaigns must not be overwritten
        # destructively by re-running ingestion.
        with LeadStore(str(self.db_path)) as store:
            custom = create_campaign(
                "Hand Authored",
                "Custom subject {{first_name}}",
                "Custom body {{company_name}}",
                campaign_id="camp_preexisting",
            )
            save_campaign(store, custom)

        self._run_ingest("camp_preexisting")

        with LeadStore(str(self.db_path)) as store:
            campaign = load_campaign(store, "camp_preexisting")
            self.assertEqual(campaign.name, "Hand Authored")
            self.assertEqual(campaign.subject_template, "Custom subject {{first_name}}")

    def test_no_real_email_is_sent_by_ingestion_or_generation(self):
        # Requirement 13: ingestion/generation must never send anything.
        # generate_pending_emails only ever persists EmailJob rows; there is
        # no SMTP/network call reachable from this path, which this test
        # documents by asserting no email_sends rows exist afterward.
        self._run_ingest("camp_no_send")
        with LeadStore(str(self.db_path)) as store:
            self.assertEqual(store.list_email_sends(campaign_id="camp_no_send"), [])


if __name__ == "__main__":
    unittest.main()
