"""Day 10: real, deterministic end-to-end integration test.

Exercises the full pipeline for 5 synthetic (fake) leads, using the *actual*
production functions from every stage — not re-implementations:

    TargetConfig -> normalize_investor_row -> LeadStore (DISCOVERED)
        -> qualify_lead (QUALIFIED / FILTERED_OUT)
        -> generate_candidates_for_lead (EMAIL_CANDIDATES_FOUND / EMAIL_NOT_FOUND)
        -> validate_and_select_email (EMAIL_VALIDATED / VALIDATION_FAILED)
        -> create_campaign + generate_email_for_lead (EMAIL_GENERATED)
        -> approve_email (APPROVED)
        -> queue_approved_email (QUEUED)
        -> send_queued_email (SENDING -> SENT / SEND_FAILED)

No real people are used (all names/companies below are synthetic/fictional),
no real network calls are made, and no real Gmail account is touched.

Mocking boundary (explicit, mirrors Day 5-9 test suites):
  - MX/SMTP checking: NullMXChecker (deterministic MX_UNKNOWN, no network)
  - Candidate generation: ScrapegraphPatternGenerator only (pure pattern
    generation from name+domain, no vendor scripts, no network)
  - Gmail SMTP: FakeSMTPClient (records calls, never opens a socket) --
    reused verbatim from test_email_sending_day8.py so send() exercises the
    exact same code path a real send would, minus the real socket.

This is a MOCKED end-to-end test. It proves the nine pipeline stages are
correctly wired together and that a lead can travel from DISCOVERED to SENT
(or a documented failure state) inside one SQLite-backed run. It does NOT
verify real Gmail authentication or a real SMTP send -- see
TEST_REPORT.md / HOW_TO_USE.md for the separate, opt-in real-Gmail
send-to-a-test-address procedure.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pipeline.lead_store import LeadStore
from pipeline.models import InvestorRow, PipelineStatus
from pipeline.lead_pipeline import ingest_discovery_rows, qualify_pending_leads
from pipeline.target_config import TargetConfig
from pipeline.email_discovery import (
    process_lead_email,
    ScrapegraphPatternGenerator,
    NullMXChecker,
)
from pipeline.email_validation import find_and_validate_pending_leads
from pipeline.campaign import create_campaign, save_campaign
from pipeline.email_generation import generate_email_for_lead, approve_email
from pipeline.email_sending import queue_approved_email, send_queued_email
from pipeline.gmail_sender import GmailSender
from pipeline.campaign_stats import get_campaign_stats

# Reuse the exact fake SMTP client from the Day 8 suite so this test never
# touches a real socket.
from pipeline.test_email_sending_day8 import FakeSMTPClient


SYNTHETIC_LEADS: list[InvestorRow] = [
    {
        "name": "Test Persona One",
        "location": "Test City, CA",
        "linkedin_url": "https://linkedin.com/in/test-persona-one",
        "profile_title": "Managing Partner",
        "summary": "Synthetic fixture for Day 10 e2e test.",
        "industries": "Fintech",
        "company_name": "Example Test Ventures",
        "source": "synthetic_test",
    },
    {
        "name": "Test Persona Two",
        "location": "Test City, CA",
        "linkedin_url": "https://linkedin.com/in/test-persona-two",
        "profile_title": "Angel Investor",
        "summary": "Synthetic fixture for Day 10 e2e test.",
        "industries": "SaaS",
        "company_name": "Example Test Capital",
        "source": "synthetic_test",
    },
    {
        # No name/company at all -> should fail candidate generation
        # (EMAIL_NOT_FOUND), exercising a documented failure branch.
        "name": "",
        "location": "Test City, CA",
        "linkedin_url": "https://linkedin.com/in/test-persona-three",
        "profile_title": "",
        "summary": "Synthetic fixture with no usable identity.",
        "industries": "SaaS",
        "company_name": "",
        "source": "synthetic_test",
    },
    {
        # Doesn't match target criteria -> FILTERED_OUT at qualification.
        "name": "Test Persona Four",
        "location": "Nowhere Relevant",
        "linkedin_url": "https://linkedin.com/in/test-persona-four",
        "profile_title": "Student",
        "summary": "Synthetic fixture that should be filtered out.",
        "industries": "Unrelated Field",
        "company_name": "Example Irrelevant Co",
        "source": "synthetic_test",
    },
    {
        "name": "Test Persona Five",
        "location": "Test City, CA",
        "linkedin_url": "https://linkedin.com/in/test-persona-five",
        "profile_title": "General Partner",
        "summary": "Synthetic fixture for Day 10 e2e test.",
        "industries": "Fintech",
        "company_name": "Example Test Ventures",
        "source": "synthetic_test",
    },
]


class TestDay10EndToEnd(unittest.TestCase):
    def setUp(self):
        self.store = LeadStore(":memory:")
        self.campaign_id = "e2e-day10-campaign"
        self.target = TargetConfig(
            locations=["Test City, CA"],
            industries=["Fintech", "SaaS"],
            target_count=10,
            name="e2e-day10",
        )

    def tearDown(self):
        self.store.close()

    def test_full_pipeline_five_synthetic_leads(self):
        store = self.store

        # 1. Discovery -> normalize + persist as DISCOVERED
        stats = ingest_discovery_rows(store, SYNTHETIC_LEADS, campaign_id=self.campaign_id)
        self.assertEqual(stats["created"], 5)

        # 2. Qualification
        qual_stats = qualify_pending_leads(store, campaign_id=self.campaign_id, target=self.target)
        self.assertEqual(qual_stats["qualified"], 4)  # persona 4 filtered out
        self.assertEqual(qual_stats["filtered_out"], 1)

        qualified_leads = store.list_by_status(PipelineStatus.QUALIFIED, campaign_id=self.campaign_id)
        self.assertEqual(len(qualified_leads), 4)

        # 3. Email candidate generation (pure pattern generator + NullMXChecker
        #    -- no network, no vendor scripts).
        for lead in qualified_leads:
            process_lead_email(
                store,
                lead,
                primary_generators=[ScrapegraphPatternGenerator()],
                supplementary_generators=[],
                fallback_generator=None,
                mx_checker=NullMXChecker(),
                enable_smtp=False,
            )

        found = store.list_by_status(PipelineStatus.EMAIL_CANDIDATES_FOUND, campaign_id=self.campaign_id)
        not_found = store.list_by_status(PipelineStatus.EMAIL_NOT_FOUND, campaign_id=self.campaign_id)
        # Persona three (no name, no company) cannot produce a candidate.
        self.assertEqual(len(not_found), 1)
        self.assertEqual(len(found), 3)

        # 4. Email validation
        val_stats = find_and_validate_pending_leads(store, campaign_id=self.campaign_id)
        validated = store.list_by_status(PipelineStatus.EMAIL_VALIDATED, campaign_id=self.campaign_id)
        self.assertEqual(len(validated), val_stats.get("validated", len(validated)))
        self.assertGreater(len(validated), 0)

        # 5. Campaign + email generation
        campaign = create_campaign(
            name="Day 10 E2E Test Campaign",
            subject_template="Quick question, {{first_name}}",
            body_template=(
                "Hi {{first_name}},\n\n"
                "This is a synthetic Day 10 test email for {{company_name}}.\n\n"
                "-- Test Sender"
            ),
            sender_name="Test Sender",
            campaign_id=self.campaign_id,
        )
        save_campaign(store, campaign)

        for lead in validated:
            generate_email_for_lead(store, lead, campaign)

        generated = store.list_by_status(PipelineStatus.EMAIL_GENERATED, campaign_id=self.campaign_id)
        self.assertEqual(len(generated), len(validated))

        # 6. Approval
        for lead in generated:
            approve_email(store, lead.lead_id)
        approved = store.list_by_status(PipelineStatus.APPROVED, campaign_id=self.campaign_id)
        self.assertEqual(len(approved), len(generated))

        # 7. Queue
        for lead in approved:
            queue_approved_email(store, lead)
        queued = store.list_by_status(PipelineStatus.QUEUED, campaign_id=self.campaign_id)
        self.assertEqual(len(queued), len(approved))

        # 8. Gmail send -- FakeSMTPClient, no real network/socket.
        gmail = GmailSender(
            address="test-sender@gmail.com",
            app_password="fake-app-password",
            smtp_client_factory=FakeSMTPClient,
        )
        for lead in queued:
            send_queued_email(store, gmail, lead.lead_id, campaign=campaign)

        sent = store.list_by_status(PipelineStatus.SENT, campaign_id=self.campaign_id)
        self.assertEqual(len(sent), len(queued))

        # A real SMTP call was never made -- the fake client recorded it instead.
        self.assertGreaterEqual(len(FakeSMTPClient.instances), 1)
        self.assertTrue(any(inst.sent for inst in FakeSMTPClient.instances))

        # 9. Statistics reflect the final state (get_campaign_stats returns
        # cumulative funnel milestones reached, plus separate failed/skipped
        # counters -- not a raw per-status histogram).
        campaign_stats = get_campaign_stats(store, self.campaign_id)
        self.assertEqual(campaign_stats["sent"], len(sent))
        self.assertEqual(campaign_stats["discovered"], 5)  # all 5 reached DISCOVERED
        self.assertEqual(campaign_stats["qualified"], 4)  # 4 reached QUALIFIED (1 filtered)
        self.assertEqual(campaign_stats["skipped"], 2)  # 1 filtered_out + 1 email_not_found

    def test_restart_does_not_resend_already_sent_email(self):
        """Crash/restart safety: re-running send against a SENT lead must not
        resend. send_queued_email requires a QUEUED EmailSend row, so once a
        lead is SENT there is nothing left to re-send."""
        store = self.store
        ingest_discovery_rows(store, SYNTHETIC_LEADS[:1], campaign_id=self.campaign_id)
        qualify_pending_leads(store, campaign_id=self.campaign_id, target=self.target)
        lead = store.list_by_status(PipelineStatus.QUALIFIED, campaign_id=self.campaign_id)[0]
        process_lead_email(
            store, lead,
            primary_generators=[ScrapegraphPatternGenerator()],
            supplementary_generators=[], fallback_generator=None,
            mx_checker=NullMXChecker(), enable_smtp=False,
        )
        lead = store.get(lead.lead_id)
        from pipeline.email_validation import validate_and_select_email
        lead = validate_and_select_email(store, lead)
        campaign = create_campaign(
            name="Restart Test", subject_template="Hi {{first_name}}",
            body_template="Hello {{first_name}}, quick note about {{company_name}}.",
            campaign_id="restart-test-campaign",
        )
        save_campaign(store, campaign)
        lead = generate_email_for_lead(store, lead, campaign)
        lead = approve_email(store, lead.lead_id)
        lead = queue_approved_email(store, lead)

        gmail = GmailSender(
            address="test-sender@gmail.com", app_password="fake-app-password",
            smtp_client_factory=FakeSMTPClient,
        )
        lead = send_queued_email(store, gmail, lead.lead_id, campaign=campaign)
        self.assertEqual(lead.pipeline_status, PipelineStatus.SENT)
        sends_before = len(FakeSMTPClient.instances)

        # Simulate "restart": re-run send_queued_email against the same lead_id.
        from pipeline.email_sending import NotQueued
        with self.assertRaises(NotQueued):
            send_queued_email(store, gmail, lead.lead_id, campaign=campaign)

        # No additional SMTP client was created / no second send happened.
        self.assertEqual(len(FakeSMTPClient.instances), sends_before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
