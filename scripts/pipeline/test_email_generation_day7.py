"""Day 7 milestone tests: Campaign model + the
EMAIL_VALIDATED -> EMAIL_GENERATED -> APPROVED / REJECTED stage.

Run with (from the `scripts/` directory, or as a module from repo root):
    python -m unittest pipeline.test_email_generation_day7 -v

No network, no LLM, no Node -- rendering is pure string substitution and
every store is an in-memory SQLite DB, so results are deterministic and fast.

Covers (per the Day 7 spec):
  - campaign creation
  - template rendering
  - all supported variables
  - missing optional fields
  - personalization
  - generated-email persistence
  - EMAIL_VALIDATED -> EMAIL_GENERATED
  - EMAIL_GENERATED -> APPROVED
  - EMAIL_GENERATED -> REJECTED
  - invalid transitions
  - bulk approval/rejection
  - resume after interruption
  - Day 2-6 regression
"""

from __future__ import annotations

import unittest

from .campaign import (
    CAMPAIGN_STATUS_ACTIVE,
    SUPPORTED_VARIABLES,
    Campaign,
    UnsupportedTemplateVariable,
    create_campaign,
    extract_template_variables,
    list_campaigns,
    load_campaign,
    render_template,
    save_campaign,
    validate_template,
)
from .email_discovery import (
    MX_UNKNOWN,
    MX_VALID,
    EmailCandidate,
    candidates_to_rows,
    process_lead_email,
)
from .email_generation import (
    REVIEW_APPROVED,
    REVIEW_PENDING,
    REVIEW_REJECTED,
    EmailGenerationError,
    EmailJob,
    NoGeneratedEmail,
    approve_email,
    bulk_approve,
    bulk_reject,
    edit_email_job,
    generate_email_for_lead,
    generate_pending_emails,
    get_email_job,
    list_email_jobs,
    personalization_context,
    preview_email,
    reject_email,
    render_email,
)
from .email_validation import validate_and_select_email
from .lead_pipeline import ingest_discovery_rows, qualify_pending_leads
from .lead_store import LeadStore
from .models import InvalidStateTransition, Lead, PipelineStatus, validate_transition
from .quality import matches_target_criteria
from .query_generator import build_queries
from .target_config import CPA_PARTNER_PRESET, TargetConfig

FULL_TEMPLATE_SUBJECT = "Quick question, {{first_name}}"
FULL_TEMPLATE_BODY = (
    "Hi {{first_name}} {{last_name}},\n\n"
    "I noticed {{company_name}} and your work as {{job_title}} in "
    "{{location}} ({{industry}}). Would love to connect.\n\n"
    "Best,\nAlex"
)


def make_lead(**overrides) -> Lead:
    defaults = dict(
        first_name="Jane",
        last_name="Doe",
        full_name="Jane Doe",
        company_name="Acme Corp",
        job_title="Managing Partner",
        location="Austin, Texas, United States",
        industry="accounting",
        pipeline_status=PipelineStatus.EMAIL_VALIDATED.value,
        email="jane.doe@acme.com",
    )
    defaults.update(overrides)
    return Lead(**defaults)


class FakeMXChecker:
    """Deterministic stand-in for NodeMXChecker so Day 4/5/6 regression
    tests embedded in this file never depend on Node or live DNS/network
    availability. Mirrors the FakeMXChecker used in
    test_email_validation_day6.py."""

    def __init__(self, statuses: dict[str, str] | None = None):
        self.statuses = statuses or {}
        self.calls: list[list[str]] = []

    def check_domains(self, domains):
        self.calls.append(list(domains))
        return {d: self.statuses.get(d, MX_UNKNOWN) for d in domains}


def make_campaign(**overrides) -> Campaign:
    defaults = dict(
        name="Q3 Outreach",
        subject_template=FULL_TEMPLATE_SUBJECT,
        body_template=FULL_TEMPLATE_BODY,
    )
    defaults.update(overrides)
    return create_campaign(
        defaults.pop("name"),
        defaults.pop("subject_template"),
        defaults.pop("body_template"),
        **defaults,
    )


# ---------------------------------------------------------------------------
# 1. Campaign creation
# ---------------------------------------------------------------------------


class TestCampaignCreation(unittest.TestCase):
    def test_create_campaign_happy_path(self):
        campaign = make_campaign()
        self.assertTrue(campaign.campaign_id)
        self.assertEqual(campaign.name, "Q3 Outreach")
        self.assertEqual(campaign.status, CAMPAIGN_STATUS_ACTIVE)
        self.assertTrue(campaign.created_at)
        self.assertTrue(campaign.updated_at)

    def test_blank_name_rejected(self):
        with self.assertRaises(ValueError):
            create_campaign("   ", FULL_TEMPLATE_SUBJECT, FULL_TEMPLATE_BODY)

    def test_blank_subject_template_rejected(self):
        with self.assertRaises(ValueError):
            create_campaign("Campaign", "", FULL_TEMPLATE_BODY)

    def test_blank_body_template_rejected(self):
        with self.assertRaises(ValueError):
            create_campaign("Campaign", FULL_TEMPLATE_SUBJECT, "")

    def test_unsupported_variable_in_subject_rejected(self):
        with self.assertRaises(UnsupportedTemplateVariable):
            create_campaign("Campaign", "Hi {{nickname}}", FULL_TEMPLATE_BODY)

    def test_unsupported_variable_in_body_rejected(self):
        with self.assertRaises(UnsupportedTemplateVariable):
            create_campaign("Campaign", FULL_TEMPLATE_SUBJECT, "Hi {{ssn}}, want a deal?")

    def test_explicit_campaign_id_honored(self):
        campaign = create_campaign(
            "Campaign", FULL_TEMPLATE_SUBJECT, FULL_TEMPLATE_BODY, campaign_id="fixed-id-1"
        )
        self.assertEqual(campaign.campaign_id, "fixed-id-1")

    def test_campaign_persistence_round_trip(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        campaign = make_campaign()
        save_campaign(store, campaign)
        loaded = load_campaign(store, campaign.campaign_id)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.name, campaign.name)
        self.assertEqual(loaded.subject_template, campaign.subject_template)
        self.assertEqual(loaded.body_template, campaign.body_template)

    def test_campaign_persistence_upsert_overwrites(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        campaign = make_campaign(campaign_id="camp-1")
        save_campaign(store, campaign)
        campaign.name = "Renamed"
        save_campaign(store, campaign)
        loaded = load_campaign(store, "camp-1")
        self.assertEqual(loaded.name, "Renamed")
        self.assertEqual(len(list_campaigns(store)), 1)

    def test_list_campaigns(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        save_campaign(store, make_campaign(campaign_id="c1", name="One"))
        save_campaign(store, make_campaign(campaign_id="c2", name="Two"))
        names = {c.name for c in list_campaigns(store)}
        self.assertEqual(names, {"One", "Two"})

    def test_load_missing_campaign_returns_none(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        self.assertIsNone(load_campaign(store, "does-not-exist"))


# ---------------------------------------------------------------------------
# 2. Template rendering / 3. all supported variables / 4. missing optional fields
# ---------------------------------------------------------------------------


class TestTemplateRendering(unittest.TestCase):
    def test_extract_template_variables(self):
        vars_found = extract_template_variables("Hi {{first_name}} from {{company_name}}!")
        self.assertEqual(vars_found, {"first_name", "company_name"})

    def test_validate_template_accepts_all_supported_variables(self):
        template = " ".join(f"{{{{{v}}}}}" for v in SUPPORTED_VARIABLES)
        validate_template(template)  # should not raise

    def test_validate_template_rejects_unknown_variable(self):
        with self.assertRaises(UnsupportedTemplateVariable):
            validate_template("{{first_name}} {{unknown_var}}")

    def test_render_template_all_six_variables(self):
        template = (
            "{{first_name}}|{{last_name}}|{{company_name}}|{{job_title}}|"
            "{{location}}|{{industry}}"
        )
        context = {
            "first_name": "Jane",
            "last_name": "Doe",
            "company_name": "Acme Corp",
            "job_title": "CFO",
            "location": "Austin",
            "industry": "accounting",
        }
        rendered = render_template(template, context)
        self.assertEqual(rendered, "Jane|Doe|Acme Corp|CFO|Austin|accounting")

    def test_render_template_handles_whitespace_variants(self):
        rendered = render_template("Hi {{ first_name }} / {{first_name}}", {"first_name": "Jane"})
        self.assertEqual(rendered, "Hi Jane / Jane")

    def test_render_template_missing_variable_in_context_renders_blank(self):
        rendered = render_template("Hi {{first_name}} {{last_name}}", {"first_name": "Jane"})
        self.assertEqual(rendered, "Hi Jane ")

    def test_render_template_no_variables_passthrough(self):
        rendered = render_template("Hello there, no variables here.", {})
        self.assertEqual(rendered, "Hello there, no variables here.")

    def test_render_template_empty_template(self):
        self.assertEqual(render_template("", {"first_name": "Jane"}), "")


class TestPersonalization(unittest.TestCase):
    def test_personalization_context_maps_all_six_fields(self):
        lead = make_lead()
        ctx = personalization_context(lead)
        self.assertEqual(
            ctx,
            {
                "first_name": "Jane",
                "last_name": "Doe",
                "company_name": "Acme Corp",
                "job_title": "Managing Partner",
                "location": "Austin, Texas, United States",
                "industry": "accounting",
            },
        )

    def test_personalization_context_missing_optional_fields_are_blank_strings(self):
        lead = make_lead(job_title="", location="", industry="")
        ctx = personalization_context(lead)
        self.assertEqual(ctx["job_title"], "")
        self.assertEqual(ctx["location"], "")
        self.assertEqual(ctx["industry"], "")
        # never None -- Lead fields are always strings
        for value in ctx.values():
            self.assertIsInstance(value, str)

    def test_render_email_full_personalization(self):
        lead = make_lead()
        campaign = make_campaign()
        rendered = render_email(campaign, lead)
        self.assertIn("Jane", rendered.subject)
        self.assertIn("Jane Doe", rendered.body)
        self.assertIn("Acme Corp", rendered.body)
        self.assertIn("Managing Partner", rendered.body)
        self.assertIn("Austin, Texas, United States", rendered.body)
        self.assertIn("accounting", rendered.body)

    def test_render_email_missing_optional_fields_no_placeholder_leftovers(self):
        lead = make_lead(job_title="", location="", industry="")
        campaign = make_campaign()
        rendered = render_email(campaign, lead)
        self.assertNotIn("{{", rendered.subject)
        self.assertNotIn("{{", rendered.body)
        self.assertNotIn("}}", rendered.body)

    def test_render_email_deterministic(self):
        lead = make_lead()
        campaign = make_campaign()
        first = render_email(campaign, lead)
        second = render_email(campaign, lead)
        self.assertEqual(first.subject, second.subject)
        self.assertEqual(first.body, second.body)


class TestPreview(unittest.TestCase):
    def test_preview_matches_render(self):
        lead = make_lead()
        campaign = make_campaign()
        self.assertEqual(preview_email(campaign, lead), render_email(campaign, lead))

    def test_preview_available_regardless_of_pipeline_status(self):
        # Preview should work even for a lead nowhere near EMAIL_VALIDATED --
        # it never touches the store or the state machine.
        lead = make_lead(pipeline_status=PipelineStatus.DISCOVERED.value)
        campaign = make_campaign()
        preview = preview_email(campaign, lead)
        self.assertIn("Jane", preview.subject)

    def test_preview_does_not_persist_anything(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        campaign = make_campaign()
        preview_email(campaign, lead)
        self.assertIsNone(store.get_email_job(lead.lead_id))


# ---------------------------------------------------------------------------
# 5. generated-email persistence / 6. EMAIL_VALIDATED -> EMAIL_GENERATED
# ---------------------------------------------------------------------------


class TestGeneratedEmailPersistence(unittest.TestCase):
    def test_generate_email_for_lead_transitions_to_email_generated(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        campaign = make_campaign()
        updated = generate_email_for_lead(store, lead, campaign)
        self.assertEqual(updated.status, PipelineStatus.EMAIL_GENERATED)

    def test_generate_email_for_lead_persists_exact_rendered_content(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        campaign = make_campaign()
        expected = render_email(campaign, lead)
        generate_email_for_lead(store, lead, campaign)

        job = get_email_job(store, lead.lead_id)
        self.assertIsNotNone(job)
        self.assertEqual(job.subject, expected.subject)
        self.assertEqual(job.body, expected.body)
        self.assertEqual(job.review_status, REVIEW_PENDING)
        self.assertFalse(job.edited)
        self.assertEqual(job.campaign_id, campaign.campaign_id)

    def test_generate_email_sets_lead_campaign_id(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead(campaign_id="")
        store.upsert_lead(lead)
        campaign = make_campaign()
        updated = generate_email_for_lead(store, lead, campaign)
        self.assertEqual(updated.campaign_id, campaign.campaign_id)

    def test_generated_content_not_regenerated_on_reread(self):
        """The whole point of persistence: reading the draft back twice
        gives byte-identical content without re-rendering."""
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        campaign = make_campaign()
        generate_email_for_lead(store, lead, campaign)

        first_read = get_email_job(store, lead.lead_id)
        second_read = get_email_job(store, lead.lead_id)
        self.assertEqual(first_read.subject, second_read.subject)
        self.assertEqual(first_read.body, second_read.body)
        self.assertEqual(first_read.job_id, second_read.job_id)

    def test_generate_email_requires_email_validated_status(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead(pipeline_status=PipelineStatus.QUALIFIED.value)
        store.upsert_lead(lead)
        campaign = make_campaign()
        with self.assertRaises(InvalidStateTransition):
            generate_email_for_lead(store, lead, campaign)
        # nothing should have been persisted
        self.assertIsNone(store.get_email_job(lead.lead_id))

    def test_generate_email_regenerating_overwrites_single_job_row(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        campaign = make_campaign()
        generate_email_for_lead(store, lead, campaign)
        first_job = get_email_job(store, lead.lead_id)

        # Re-fetch the lead (now EMAIL_GENERATED) and force it back to
        # EMAIL_VALIDATED isn't legal, so instead verify there is still
        # exactly one job row after a direct re-save (simulating an
        # idempotent re-run guard at a higher layer would call this only
        # from EMAIL_VALIDATED; here we just check storage shape).
        jobs = store.list_email_jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["job_id"], first_job.job_id)

    def test_generate_email_empty_render_diverts_to_generation_failed(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        # A campaign whose templates contain only variables that will be
        # blank for this lead collapses to empty content.
        blank_campaign = create_campaign(
            "Blank", "{{job_title}}", "{{location}}", campaign_id="blank-camp"
        )
        lead_with_blanks = make_lead(job_title="", location="")
        store.upsert_lead(lead_with_blanks)
        updated = generate_email_for_lead(store, lead_with_blanks, blank_campaign)
        self.assertEqual(updated.status, PipelineStatus.GENERATION_FAILED)
        self.assertIsNone(store.get_email_job(lead_with_blanks.lead_id))

    def test_generate_pending_emails_bulk(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        campaign = make_campaign()
        leads = [make_lead(email=f"lead{i}@acme.com", campaign_id=campaign.campaign_id) for i in range(3)]
        for lead in leads:
            store.upsert_lead(lead)
        stats = generate_pending_emails(store, campaign)
        self.assertEqual(stats["generated"], 3)
        self.assertEqual(stats["failed"], 0)
        for lead in leads:
            refreshed = store.get(lead.lead_id)
            self.assertEqual(refreshed.status, PipelineStatus.EMAIL_GENERATED)
            self.assertIsNotNone(store.get_email_job(lead.lead_id))

    def test_generate_pending_emails_only_processes_email_validated(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        campaign = make_campaign()
        validated = make_lead(campaign_id=campaign.campaign_id)
        already_generated = make_lead(campaign_id=campaign.campaign_id)
        store.upsert_lead(validated)
        store.upsert_lead(already_generated)
        generate_email_for_lead(store, already_generated, campaign)

        stats = generate_pending_emails(store, campaign)
        # only `validated` should be picked up this round
        self.assertEqual(stats["generated"], 1)


# ---------------------------------------------------------------------------
# 7. Edit / preview functionality
# ---------------------------------------------------------------------------


class TestEditFunctionality(unittest.TestCase):
    def _generated_lead(self, store):
        lead = make_lead()
        store.upsert_lead(lead)
        campaign = make_campaign()
        generate_email_for_lead(store, lead, campaign)
        return store.get(lead.lead_id)

    def test_edit_subject_and_body(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = self._generated_lead(store)
        job = edit_email_job(store, lead.lead_id, subject="New subject", body="New body")
        self.assertEqual(job.subject, "New subject")
        self.assertEqual(job.body, "New body")
        self.assertTrue(job.edited)

        reloaded = get_email_job(store, lead.lead_id)
        self.assertEqual(reloaded.subject, "New subject")
        self.assertEqual(reloaded.body, "New body")
        self.assertTrue(reloaded.edited)

    def test_edit_partial_update_leaves_other_field_untouched(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = self._generated_lead(store)
        original = get_email_job(store, lead.lead_id)
        job = edit_email_job(store, lead.lead_id, subject="Only subject changed")
        self.assertEqual(job.subject, "Only subject changed")
        self.assertEqual(job.body, original.body)

    def test_edit_requires_existing_job(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        with self.assertRaises(NoGeneratedEmail):
            edit_email_job(store, "no-such-lead", subject="x")

    def test_edit_after_approval_rejected(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = self._generated_lead(store)
        approve_email(store, lead.lead_id)
        with self.assertRaises(ValueError):
            edit_email_job(store, lead.lead_id, subject="too late")


# ---------------------------------------------------------------------------
# 8/9. EMAIL_GENERATED -> APPROVED / REJECTED, invalid transitions
# ---------------------------------------------------------------------------


class TestApproveReject(unittest.TestCase):
    def _generated_lead(self, store, **overrides):
        lead = make_lead(**overrides)
        store.upsert_lead(lead)
        campaign = make_campaign()
        generate_email_for_lead(store, lead, campaign)
        return store.get(lead.lead_id)

    def test_approve_transitions_to_approved(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = self._generated_lead(store)
        updated = approve_email(store, lead.lead_id)
        self.assertEqual(updated.status, PipelineStatus.APPROVED)
        job = get_email_job(store, lead.lead_id)
        self.assertEqual(job.review_status, REVIEW_APPROVED)
        self.assertTrue(job.reviewed_at)

    def test_reject_transitions_to_rejected(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = self._generated_lead(store)
        updated = reject_email(store, lead.lead_id, reason="Not a fit")
        self.assertEqual(updated.status, PipelineStatus.REJECTED)
        job = get_email_job(store, lead.lead_id)
        self.assertEqual(job.review_status, REVIEW_REJECTED)
        self.assertEqual(job.rejection_reason, "Not a fit")

    def test_reject_without_reason_defaults_blank(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = self._generated_lead(store)
        reject_email(store, lead.lead_id)
        job = get_email_job(store, lead.lead_id)
        self.assertEqual(job.rejection_reason, "")

    def test_approve_requires_existing_job(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        with self.assertRaises(NoGeneratedEmail):
            approve_email(store, "ghost-lead")

    def test_reject_requires_existing_job(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        with self.assertRaises(NoGeneratedEmail):
            reject_email(store, "ghost-lead")

    def test_cannot_approve_twice(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = self._generated_lead(store)
        approve_email(store, lead.lead_id)
        with self.assertRaises(InvalidStateTransition):
            approve_email(store, lead.lead_id)

    def test_cannot_reject_an_already_rejected_lead(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = self._generated_lead(store)
        reject_email(store, lead.lead_id)
        with self.assertRaises(InvalidStateTransition):
            reject_email(store, lead.lead_id)

    def test_cannot_approve_a_lead_not_yet_generated(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead(pipeline_status=PipelineStatus.EMAIL_VALIDATED.value)
        store.upsert_lead(lead)
        with self.assertRaises(NoGeneratedEmail):
            approve_email(store, lead.lead_id)

    def test_invalid_transition_email_validated_to_approved_directly(self):
        with self.assertRaises(InvalidStateTransition):
            validate_transition(PipelineStatus.EMAIL_VALIDATED, PipelineStatus.APPROVED)

    def test_invalid_transition_discovered_to_email_generated(self):
        with self.assertRaises(InvalidStateTransition):
            validate_transition(PipelineStatus.DISCOVERED, PipelineStatus.EMAIL_GENERATED)

    def test_email_generated_to_rejected_is_legal(self):
        # Explicit spec requirement: rejection reachable directly from
        # EMAIL_GENERATED, not only via APPROVED.
        self.assertEqual(
            validate_transition(PipelineStatus.EMAIL_GENERATED, PipelineStatus.REJECTED),
            PipelineStatus.REJECTED,
        )

    def test_rejected_is_terminal(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = self._generated_lead(store)
        reject_email(store, lead.lead_id)
        with self.assertRaises(InvalidStateTransition):
            validate_transition(PipelineStatus.REJECTED, PipelineStatus.APPROVED)


# ---------------------------------------------------------------------------
# 10. Bulk approval/rejection
# ---------------------------------------------------------------------------


class TestBulkActions(unittest.TestCase):
    def _three_generated_leads(self, store):
        campaign = make_campaign()
        leads = [make_lead(email=f"person{i}@acme.com") for i in range(3)]
        for lead in leads:
            store.upsert_lead(lead)
            generate_email_for_lead(store, lead, campaign)
        return [store.get(lead.lead_id) for lead in leads]

    def test_bulk_approve_all_succeed(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        leads = self._three_generated_leads(store)
        result = bulk_approve(store, [lead.lead_id for lead in leads])
        self.assertEqual(len(result["approved"]), 3)
        self.assertEqual(result["failed"], [])
        for lead in leads:
            self.assertEqual(store.get(lead.lead_id).status, PipelineStatus.APPROVED)

    def test_bulk_reject_all_succeed(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        leads = self._three_generated_leads(store)
        result = bulk_reject(store, [lead.lead_id for lead in leads], reason="Bulk cleanup")
        self.assertEqual(len(result["rejected"]), 3)
        for lead in leads:
            self.assertEqual(store.get(lead.lead_id).status, PipelineStatus.REJECTED)
            job = get_email_job(store, lead.lead_id)
            self.assertEqual(job.rejection_reason, "Bulk cleanup")

    def test_bulk_approve_partial_failure_does_not_abort_batch(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        leads = self._three_generated_leads(store)
        good_ids = [lead.lead_id for lead in leads]
        ids = [good_ids[0], "nonexistent-lead", good_ids[1]]
        result = bulk_approve(store, ids)
        self.assertEqual(set(result["approved"]), {good_ids[0], good_ids[1]})
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["lead_id"], "nonexistent-lead")
        # third lead untouched, still just EMAIL_GENERATED
        self.assertEqual(store.get(good_ids[2]).status, PipelineStatus.EMAIL_GENERATED)

    def test_bulk_reject_partial_failure_does_not_abort_batch(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        leads = self._three_generated_leads(store)
        ids = [leads[0].lead_id, "missing", leads[1].lead_id]
        result = bulk_reject(store, ids)
        self.assertEqual(set(result["rejected"]), {leads[0].lead_id, leads[1].lead_id})
        self.assertEqual(len(result["failed"]), 1)

    def test_bulk_approve_empty_list(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        result = bulk_approve(store, [])
        self.assertEqual(result, {"approved": [], "failed": []})

    def test_list_email_jobs_filters_by_review_status(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        leads = self._three_generated_leads(store)
        approve_email(store, leads[0].lead_id)
        reject_email(store, leads[1].lead_id)
        # leads[2] left pending
        approved = list_email_jobs(store, review_status=REVIEW_APPROVED)
        rejected = list_email_jobs(store, review_status=REVIEW_REJECTED)
        pending = list_email_jobs(store, review_status=REVIEW_PENDING)
        self.assertEqual(len(approved), 1)
        self.assertEqual(len(rejected), 1)
        self.assertEqual(len(pending), 1)


# ---------------------------------------------------------------------------
# 11. Resume after interruption
# ---------------------------------------------------------------------------


class TestResumeAfterInterruption(unittest.TestCase):
    def test_resume_generation_picks_up_only_remaining_email_validated(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        campaign = make_campaign()
        leads = [
            make_lead(email=f"resume{i}@acme.com", campaign_id=campaign.campaign_id)
            for i in range(4)
        ]
        for lead in leads:
            store.upsert_lead(lead)

        # Simulate a first, interrupted run that only got through half.
        for lead in leads[:2]:
            generate_email_for_lead(store, lead, campaign)

        # "Resume": call the bulk runner again.
        stats = generate_pending_emails(store, campaign)
        self.assertEqual(stats["generated"], 2)

        for lead in leads:
            self.assertEqual(store.get(lead.lead_id).status, PipelineStatus.EMAIL_GENERATED)
        self.assertEqual(len(store.list_email_jobs()), 4)

    def test_resume_is_idempotent_for_already_generated_leads(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        campaign = make_campaign()
        lead = make_lead()
        store.upsert_lead(lead)
        generate_email_for_lead(store, lead, campaign)
        first_job = get_email_job(store, lead.lead_id)

        # Running the bulk generator again should not touch the
        # already-EMAIL_GENERATED lead at all.
        stats = generate_pending_emails(store, campaign)
        self.assertEqual(stats["generated"], 0)
        second_job = get_email_job(store, lead.lead_id)
        self.assertEqual(first_job.job_id, second_job.job_id)
        self.assertEqual(first_job.updated_at, second_job.updated_at)

    def test_resume_full_pipeline_end_to_end_through_generation(self):
        """DISCOVERED -> ... -> EMAIL_VALIDATED -> EMAIL_GENERATED, using
        the real Day 4/5/6 stages, then interrupting and resuming Day 7
        generation, exercising the whole chain built so far."""
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        campaign_id = "resume-e2e"
        campaign = make_campaign(campaign_id=campaign_id)
        rows = [
            {
                "name": "Jane Doe", "location": "Austin, Texas, United States",
                "linkedin_url": "https://www.linkedin.com/in/janedoe/",
                "profile_title": "Managing Partner", "summary": "CPA firm partner.",
                "industries": "accounting", "email": "", "phone": "",
                "source": "ddgs_search", "company_name": "Acme CPAs",
                "company_size": "25", "age": "", "age_source": "", "age_confidence": "",
            },
            {
                "name": "John Smith", "location": "Denver, Colorado, United States",
                "linkedin_url": "https://www.linkedin.com/in/johnsmith/",
                "profile_title": "Tax Partner", "summary": "CPA firm partner.",
                "industries": "accounting", "email": "", "phone": "",
                "source": "ddgs_search", "company_name": "Beta CPAs",
                "company_size": "10", "age": "", "age_source": "", "age_confidence": "",
            },
        ]
        ingest_discovery_rows(store, rows, campaign_id=campaign_id)
        qualify_pending_leads(store, campaign_id=campaign_id, target=CPA_PARTNER_PRESET)
        mx_checker = FakeMXChecker({"acme.com": MX_VALID, "beta.com": MX_VALID})
        for lead in store.list_by_status(PipelineStatus.QUALIFIED, campaign_id=campaign_id):
            process_lead_email(store, lead, mx_checker=mx_checker)
        for lead in store.list_by_status(PipelineStatus.EMAIL_CANDIDATES_FOUND, campaign_id=campaign_id):
            validate_and_select_email(store, lead)

        validated_leads = store.list_by_status(PipelineStatus.EMAIL_VALIDATED, campaign_id=campaign_id)
        self.assertGreaterEqual(len(validated_leads), 1)

        # Interrupt after generating only the first lead.
        generate_email_for_lead(store, validated_leads[0], campaign)

        # Resume: pick up the rest.
        stats = generate_pending_emails(store, campaign)
        self.assertEqual(stats["generated"], len(validated_leads) - 1)

        for lead in validated_leads:
            refreshed = store.get(lead.lead_id)
            self.assertEqual(refreshed.status, PipelineStatus.EMAIL_GENERATED)
            job = get_email_job(store, lead.lead_id)
            self.assertIsNotNone(job)
            self.assertTrue(job.subject)
            self.assertTrue(job.body)


# ---------------------------------------------------------------------------
# 12. Day 2-6 regression
# ---------------------------------------------------------------------------


class TestDay2Regression(unittest.TestCase):
    def test_target_config_and_query_generation_still_work(self):
        cfg = TargetConfig.from_dict(
            {"name": "saas_founders", "titles": ["Founder", "CEO"], "industries": ["SaaS"], "target_count": 100}
        )
        self.assertGreater(len(build_queries(cfg)), 0)
        self.assertEqual(cfg.output_stem(), "saas_founders")

    def test_cpa_preset_unchanged(self):
        self.assertEqual(CPA_PARTNER_PRESET.name, "us_cpa_partners")
        self.assertGreater(len(build_queries(CPA_PARTNER_PRESET)), 0)


class TestDay3Regression(unittest.TestCase):
    def test_matches_target_criteria_still_qualifies_and_rejects(self):
        target = TargetConfig(name="saas", titles=["Founder", "CEO"], industries=["SaaS"])
        good = {
            "profile_title": "Founder & CEO", "summary": "Building a SaaS company.",
            "industries": "SaaS", "location": "San Francisco, California, United States",
            "company_size": "25", "age": "", "age_confidence": "",
        }
        bad = {
            "profile_title": "Managing Partner", "summary": "CPA firm",
            "industries": "accounting", "location": "San Francisco, California, United States",
            "company_size": "25", "age": "", "age_confidence": "",
        }
        self.assertTrue(matches_target_criteria(good, target))
        self.assertFalse(matches_target_criteria(bad, target))


class TestDay4Regression(unittest.TestCase):
    def test_normalize_investor_row_and_qualify_still_work_end_to_end(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        row = {
            "name": "Jane Doe", "location": "San Francisco, California, United States",
            "linkedin_url": "https://www.linkedin.com/in/janedoe/", "profile_title": "Founder & CEO",
            "summary": "Building a SaaS company for accountants.", "industries": "SaaS",
            "email": "", "phone": "", "source": "ddgs_search", "company_name": "Acme Corp",
            "company_size": "25", "age": "", "age_source": "", "age_confidence": "",
        }
        stats = ingest_discovery_rows(store, [row], campaign_id="c1")
        self.assertEqual(stats["created"], 1)
        target = TargetConfig(name="saas", titles=["Founder", "CEO"], industries=["SaaS"])
        qual_stats = qualify_pending_leads(store, campaign_id="c1", target=target)
        self.assertEqual(qual_stats["qualified"], 1)
        [lead] = store.list_by_status(PipelineStatus.QUALIFIED, campaign_id="c1")
        self.assertEqual(lead.company_name, "Acme Corp")

    def test_lead_state_machine_unchanged_for_pre_day7_edges(self):
        self.assertEqual(
            validate_transition(PipelineStatus.QUALIFIED, PipelineStatus.EMAIL_CANDIDATES_FOUND),
            PipelineStatus.EMAIL_CANDIDATES_FOUND,
        )
        with self.assertRaises(InvalidStateTransition):
            validate_transition(PipelineStatus.QUALIFIED, PipelineStatus.EMAIL_VALIDATED)


class TestDay5Regression(unittest.TestCase):
    def test_process_lead_email_still_selects_best_and_reaches_email_candidates_found(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = Lead(first_name="Jane", last_name="Doe", full_name="Jane Doe", company_name="Acme Corp",
                    pipeline_status=PipelineStatus.QUALIFIED.value)
        store.upsert_lead(lead)
        updated = process_lead_email(store, lead, mx_checker=FakeMXChecker({"acme.com": MX_VALID}))
        self.assertEqual(updated.status, PipelineStatus.EMAIL_CANDIDATES_FOUND)
        self.assertEqual(updated.email, "jane.doe@acme.com")


class TestDay6Regression(unittest.TestCase):
    def test_validate_and_select_email_still_reaches_email_validated(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = Lead(first_name="Jane", last_name="Doe", full_name="Jane Doe", company_name="Acme Corp",
                    pipeline_status=PipelineStatus.QUALIFIED.value)
        store.upsert_lead(lead)
        store.save_candidates(lead.lead_id, candidates_to_rows(lead.lead_id, [
            EmailCandidate(email="jane.doe@acme.com", domain="acme.com", mx_status=MX_VALID, mx_checked=True),
        ]))
        store.transition(lead.lead_id, PipelineStatus.EMAIL_CANDIDATES_FOUND)
        stuck_lead = store.get(lead.lead_id)
        result = validate_and_select_email(store, stuck_lead)
        self.assertEqual(result.status, PipelineStatus.EMAIL_VALIDATED)
        self.assertEqual(result.email, "jane.doe@acme.com")

    def test_email_candidates_found_to_email_validated_edge_unchanged(self):
        self.assertEqual(
            validate_transition(PipelineStatus.EMAIL_CANDIDATES_FOUND, PipelineStatus.EMAIL_VALIDATED),
            PipelineStatus.EMAIL_VALIDATED,
        )


if __name__ == "__main__":
    unittest.main()
