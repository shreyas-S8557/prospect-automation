"""Regression tests for evidence-grounded email personalization and the
pre-EMAIL_GENERATED quality safeguards.

Background: the previous DEFAULT_SUBJECT_TEMPLATE/DEFAULT_BODY_TEMPLATE
("Quick question, {{first_name}}" / "I came across {{company_name}} and
wanted to reach out.") blindly spliced Lead.company_name into outreach
copy with no validation -- so a university ("RIT"), a bare industry term
("AI"), or a garbled scraped headline could end up presented as fact in a
real email. This file tests the fix:

  - is_confident_company() / clean_job_title() / display_person_name() /
    compose_personalization() (pipeline/email_generation.py): build
    evidence-grounded personalization, never presenting a company/role
    that isn't actually supported by the lead's own data.
  - assess_email_quality() (pipeline/email_generation.py): a final,
    independent safeguard that must pass before a lead is allowed to reach
    EMAIL_GENERATED; failures divert to GENERATION_FAILED with a reason.

Run with (from the `scripts/` directory):
    python -m unittest pipeline.test_email_personalization_quality -v

No network, no LLM. The four leads at the top of this file are the exact
records that were actually in data/saas_test2.db when this bug was
reported (fields copied verbatim from that database), so these tests
exercise the real reported scenarios, not synthetic look-alikes.
"""

from __future__ import annotations

import unittest

from pipeline.campaign import (
    DEFAULT_BODY_TEMPLATE,
    DEFAULT_SUBJECT_TEMPLATE,
    create_campaign,
)
from pipeline.email_generation import (
    RenderedEmail,
    assess_email_quality,
    clean_job_title,
    compose_personalization,
    display_person_name,
    format_industry_list,
    generate_email_for_lead,
    is_confident_company,
    primary_industry_term,
    render_email,
)
from pipeline.lead_store import LeadStore
from pipeline.models import Lead, PipelineStatus

# ---------------------------------------------------------------------------
# The four real leads from the bug report (data/saas_test2.db), reproduced
# verbatim from `store.get(lead_id).to_dict()` at the time it was filed.
# ---------------------------------------------------------------------------


def lead_adib() -> Lead:
    return Lead(
        first_name="Adib", last_name="Ahnaf", full_name="Adib Ahnaf",
        job_title="Founder/COO", company_name="RIT", company_domain="rit.com",
        industry="SaaS; AI; Automation", location="Rochester, NY, USA",
        linkedin_url="https://www.linkedin.com/in/adibahnaf",
        profile_summary=(
            "I\u2019m a junior MIS student at RIT and the founder/COO of a "
            "venture-backed AI SaaS startup\u2026"
        ),
        email="adib.ahnaf@rit.com", email_status="validated",
        pipeline_status=PipelineStatus.EMAIL_VALIDATED.value,
    )


def lead_jatin() -> Lead:
    return Lead(
        first_name="Jatin", last_name="kumar", full_name="Jatin kumar",
        job_title="Jatin kumar - Hashbyt - AI-First Leading Frontend & UI/UX SaaS Partner",
        company_name="Hashbyt", company_domain="hashbyt.com",
        industry="AI SaaS", location="UX SaaS Partner Tampa, Florida, United States",
        linkedin_url="https://www.linkedin.com/in/jatin-kumar-864682378",
        profile_summary=(
            "Tampa, Florida, United States. 61 followers 59 connections. See "
            "your mutual connections. View mutual connections with Jatin. "
            "Jatin can introduce you to 3 people at Hashbyt - AI-First "
            "Leading Frontend & UI/UX SaaS Partner. Email or phone."
        ),
        email="jatin.kumar@hashbyt.com", email_status="validated",
        pipeline_status=PipelineStatus.EMAIL_VALIDATED.value,
    )


def lead_krishna() -> Lead:
    return Lead(
        first_name="Krishna", last_name="Parimi", full_name="Krishna Parimi",
        job_title="CEO", company_name="EzData", company_domain="ezdata.ai",
        industry="SaaS; Artificial Intelligence; Automation",
        location="San Jose, CA, USA",
        linkedin_url="https://www.linkedin.com/in/krishna-parimi",
        profile_summary=(
            "CEO \u00b7 Silicon Valley entrepreneur with a record of building "
            "and scaling tech startups; now leading EzData\u2019s AI\u2011native "
            "solutions."
        ),
        email="krishna.parimi@ezdata.ai", email_status="validated",
        pipeline_status=PipelineStatus.EMAIL_VALIDATED.value,
    )


def lead_mchael() -> Lead:
    return Lead(
        first_name="MCHAEL", last_name="RODRIGUEZ", full_name="MCHAEL RODRIGUEZ",
        job_title="CEO", company_name="AI", company_domain="ai.com",
        industry="SaaS; AI; Automation",
        location="Rancho Cordova, California, United States",
        linkedin_url="https://www.linkedin.com/in/mchael-rodriguez-3a137556",
        profile_summary=(
            "We\u2019re unlocking community knowledge in a new way. Experts "
            "add insights directly into each article, started with the "
            "help of AI."
        ),
        email="mchael.rodriguez@ai.com", email_status="validated",
        pipeline_status=PipelineStatus.EMAIL_VALIDATED.value,
    )


def default_campaign(**overrides):
    return create_campaign(
        overrides.pop("name", "SaaS AI Founders"),
        overrides.pop("subject_template", DEFAULT_SUBJECT_TEMPLATE),
        overrides.pop("body_template", DEFAULT_BODY_TEMPLATE),
        campaign_id=overrides.pop("campaign_id", "saas_ai_founders_test"),
        **overrides,
    )


# ---------------------------------------------------------------------------
# 1. Company-confidence checks
# ---------------------------------------------------------------------------


class TestIsConfidentCompany(unittest.TestCase):
    def test_university_extracted_as_company_is_rejected(self):
        # "RIT" is a plausible-looking bare company name on its own, but the
        # lead's own evidence ("junior MIS student at RIT") shows it's the
        # school, not the employer.
        self.assertFalse(is_confident_company(lead_adib()))

    def test_generic_industry_term_as_company_is_rejected(self):
        self.assertFalse(is_confident_company(lead_mchael()))

    def test_missing_company_is_not_confident(self):
        lead = lead_krishna()
        lead.company_name = ""
        self.assertFalse(is_confident_company(lead))

    def test_valid_grounded_company_is_confident(self):
        self.assertTrue(is_confident_company(lead_krishna()))
        self.assertTrue(is_confident_company(lead_jatin()))

    def test_spelled_out_university_still_rejected_by_existing_markers(self):
        lead = lead_adib()
        lead.company_name = "Rochester Institute of Technology"
        self.assertFalse(is_confident_company(lead))

    def test_short_legitimate_company_without_enrollment_context_is_confident(self):
        # A short/ambiguous-looking name is fine when nothing in the
        # evidence marks it as an educational institution -- this guards
        # against the enrollment check being overly broad.
        lead = Lead(
            first_name="Sam", last_name="Lee", job_title="CTO",
            company_name="Vox", profile_summary="CTO leading engineering at Vox.",
        )
        self.assertTrue(is_confident_company(lead))


# ---------------------------------------------------------------------------
# 2. Job title cleaning
# ---------------------------------------------------------------------------


class TestCleanJobTitle(unittest.TestCase):
    def test_strips_duplicated_name_and_company_prefix(self):
        cleaned = clean_job_title(
            "Jatin kumar - Hashbyt - AI-First Leading Frontend & UI/UX SaaS Partner",
            "Jatin kumar", "Hashbyt",
        )
        self.assertEqual(cleaned, "AI-First Leading Frontend & UI/UX SaaS Partner")

    def test_clean_title_left_unchanged(self):
        self.assertEqual(clean_job_title("CEO", "Krishna Parimi", "EzData"), "CEO")

    def test_empty_title_stays_empty(self):
        self.assertEqual(clean_job_title("", "Jane Doe", "Acme"), "")

    def test_pathologically_long_title_dropped_rather_than_used_verbatim(self):
        long_title = "A very long run-on headline " * 6
        self.assertEqual(clean_job_title(long_title, "Jane Doe", "Acme"), "")


# ---------------------------------------------------------------------------
# 3. Name normalization
# ---------------------------------------------------------------------------


class TestDisplayPersonName(unittest.TestCase):
    def test_all_caps_name_is_title_cased_not_reinvented(self):
        # Normalized for professional display -- never guessed at as a
        # "corrected" spelling like "Michael".
        self.assertEqual(display_person_name("MCHAEL"), "Mchael")
        self.assertNotEqual(display_person_name("MCHAEL"), "Michael")

    def test_all_lowercase_name_is_title_cased(self):
        self.assertEqual(display_person_name("jatin"), "Jatin")

    def test_normal_mixed_case_name_is_left_alone(self):
        self.assertEqual(display_person_name("Jane"), "Jane")

    def test_invalid_name_returns_empty(self):
        self.assertEqual(display_person_name(""), "")
        self.assertEqual(display_person_name("http://spam.example"), "")


# ---------------------------------------------------------------------------
# 4. compose_personalization -- evidence-grounded opening/subject/value
# ---------------------------------------------------------------------------


class TestComposePersonalization(unittest.TestCase):
    def test_university_never_appears_as_company_in_opening_or_subject(self):
        result = compose_personalization(lead_adib())
        self.assertNotIn("RIT", result.opening_line)
        self.assertNotIn("RIT", result.subject_hook)
        self.assertEqual(result.company_used, "")
        # Falls back to the genuinely grounded evidence instead: role + industry.
        self.assertIn("Founder/COO", result.opening_line)

    def test_generic_ai_never_appears_as_company(self):
        result = compose_personalization(lead_mchael())
        self.assertEqual(result.company_used, "")
        self.assertNotIn("I came across AI", result.opening_line)
        # Falls back to role + industry (both genuinely present on the lead).
        self.assertIn("CEO", result.opening_line)
        self.assertIn("SaaS", result.opening_line)

    def test_valid_company_is_used_and_role_cleaned(self):
        result = compose_personalization(lead_jatin())
        self.assertEqual(result.company_used, "Hashbyt")
        self.assertIn("Hashbyt", result.opening_line)
        self.assertNotIn("Jatin kumar - Hashbyt -", result.opening_line)

    def test_confident_company_and_role_grounds_krishna(self):
        result = compose_personalization(lead_krishna())
        self.assertEqual(result.company_used, "EzData")
        self.assertIn("CEO", result.opening_line)
        self.assertIn("EzData", result.opening_line)
        self.assertIn(("role", "company"), [tuple(result.evidence_used)])

    def test_insufficient_evidence_falls_back_honestly(self):
        lead = Lead(first_name="Pat", last_name="Doe")
        result = compose_personalization(lead)
        self.assertTrue(result.fallback)
        self.assertEqual(result.company_used, "")
        self.assertEqual(result.role_used, "")
        # Still a coherent, honest sentence -- not an empty/broken template.
        self.assertIn("your profile", result.opening_line)
        self.assertNotIn("{{", result.opening_line)

    def test_subject_never_bare_generic_when_evidence_exists(self):
        for lead in (lead_adib(), lead_jatin(), lead_krishna(), lead_mchael()):
            result = compose_personalization(lead)
            self.assertNotRegex(result.subject_hook, r"^Quick question,\s*\w+$")

    def test_subject_bare_generic_only_when_genuinely_no_evidence(self):
        lead = Lead(first_name="Pat", last_name="")
        result = compose_personalization(lead)
        self.assertEqual(result.subject_hook, "Quick question, Pat")


# ---------------------------------------------------------------------------
# 5. assess_email_quality -- the final pre-EMAIL_GENERATED safeguard
# ---------------------------------------------------------------------------


class TestAssessEmailQuality(unittest.TestCase):
    def test_well_grounded_email_passes(self):
        lead = lead_krishna()
        rendered = render_email(default_campaign(), lead)
        self.assertEqual(assess_email_quality(lead, rendered), [])

    def test_unresolved_placeholder_is_rejected(self):
        lead = lead_krishna()
        rendered = RenderedEmail(subject="Hi {{first_name}}", body="Body text.")
        issues = assess_email_quality(lead, rendered)
        self.assertTrue(any("placeholder" in i for i in issues))

    def test_unconfirmed_company_leaking_into_draft_is_rejected(self):
        lead = lead_adib()  # RIT is not confident
        rendered = RenderedEmail(
            subject="Quick note", body="I saw your work at RIT and wanted to connect."
        )
        issues = assess_email_quality(lead, rendered)
        self.assertTrue(any("RIT" in i for i in issues))

    def test_generic_rejected_company_name_mentioned_via_industry_is_not_flagged(self):
        # MCHAEL's company_name "AI" was rejected as too generic to be a
        # real employer -- but "AI" is also his genuine industry, so a
        # grounded email that legitimately talks about his AI/SaaS work
        # must NOT be rejected just because the substring "AI" appears.
        lead = lead_mchael()
        rendered = RenderedEmail(
            subject="Quick question about your SaaS work",
            body="Hi Mchael, I noticed your work as CEO in SaaS, AI and Automation.",
        )
        issues = assess_email_quality(lead, rendered)
        self.assertEqual(issues, [])

    def test_unsupported_claim_is_rejected(self):
        lead = lead_krishna()
        rendered = RenderedEmail(
            subject="Congrats on your Series B",
            body="Congrats on your Series B funding round -- huge news!",
        )
        issues = assess_email_quality(lead, rendered)
        self.assertTrue(any("series b" in i for i in issues))

    def test_claim_supported_by_evidence_is_not_flagged(self):
        # Mentioning "AI" is fine -- it's literally in this lead's own
        # industry field -- and isn't one of the funding/press/etc claim
        # markers _has_unsupported_claim looks for in the first place, so
        # referencing it (alongside real grounding: role + company) must
        # not be rejected as an "unsupported claim".
        lead = lead_krishna()
        rendered = RenderedEmail(
            subject="Quick question about EzData",
            body="Hi Krishna, your CEO role at EzData and AI-native work looks interesting.",
        )
        issues = assess_email_quality(lead, rendered)
        self.assertEqual(issues, [])

    def test_insane_recipient_name_is_rejected(self):
        lead = Lead(first_name="https://spam.example", last_name="Doe")
        rendered = RenderedEmail(subject="Hi", body="Body text with enough content.")
        issues = assess_email_quality(lead, rendered)
        self.assertTrue(any("valid person name" in i for i in issues))

    def test_generic_email_with_available_evidence_is_rejected(self):
        # Reproduces the exact original bug: rich evidence available
        # (role/company/industry) but the drafted email reflects none of it.
        lead = lead_krishna()
        rendered = RenderedEmail(
            subject="Quick question, Krishna",
            body="Hi Krishna,\n\nI came across your profile and wanted to reach out.\n\nBest",
        )
        issues = assess_email_quality(lead, rendered)
        self.assertTrue(issues)

    def test_generic_fallback_with_no_evidence_at_all_is_accepted(self):
        # Requirement: a safe, honest generic fallback for a lead with
        # genuinely no personalization evidence must NOT be rejected.
        lead = Lead(first_name="Pat", last_name="Doe")
        rendered = RenderedEmail(
            subject="Quick question, Pat",
            body="Hi Pat,\n\nI came across your profile and wanted to reach out.\n\nBest",
        )
        issues = assess_email_quality(lead, rendered)
        self.assertEqual(issues, [])


# ---------------------------------------------------------------------------
# 6. Full generate_email_for_lead behavior against the four real leads
# ---------------------------------------------------------------------------


class TestGenerateEmailForRealLeads(unittest.TestCase):
    def setUp(self):
        self.store = LeadStore(":memory:")
        self.addCleanup(self.store.close)
        self.campaign = default_campaign()

    def _generate(self, lead: Lead) -> tuple[Lead, dict | None]:
        self.store.upsert_lead(lead)
        updated = generate_email_for_lead(self.store, lead, self.campaign)
        job = self.store.get_email_job(lead.lead_id)
        return updated, job

    def test_adib_generates_without_claiming_rit_as_company(self):
        updated, job = self._generate(lead_adib())
        self.assertEqual(updated.status, PipelineStatus.EMAIL_GENERATED)
        self.assertNotIn("RIT", job["subject"])
        self.assertNotIn("RIT", job["body"])
        self.assertIn("Founder/COO", job["body"])

    def test_jatin_generates_with_hashbyt_as_company(self):
        updated, job = self._generate(lead_jatin())
        self.assertEqual(updated.status, PipelineStatus.EMAIL_GENERATED)
        self.assertIn("Hashbyt", job["body"])

    def test_krishna_generates_with_ezdata_and_role(self):
        updated, job = self._generate(lead_krishna())
        self.assertEqual(updated.status, PipelineStatus.EMAIL_GENERATED)
        self.assertIn("EzData", job["body"])
        self.assertIn("CEO", job["body"])

    def test_mchael_generates_without_claiming_ai_as_company(self):
        updated, job = self._generate(lead_mchael())
        self.assertEqual(updated.status, PipelineStatus.EMAIL_GENERATED)
        self.assertNotIn("I came across AI", job["body"])
        # Greeting uses a professionally-cased name, not a raw ALL-CAPS dump.
        self.assertIn("Mchael", job["body"])
        self.assertNotIn("Hi MCHAEL,", job["body"])

    def test_no_draft_ever_contains_a_placeholder(self):
        for lead_factory in (lead_adib, lead_jatin, lead_krishna, lead_mchael):
            _, job = self._generate(lead_factory())
            self.assertNotIn("{{", job["subject"])
            self.assertNotIn("{{", job["body"])


# ---------------------------------------------------------------------------
# 7. Existing valid generation behavior still works (Day 7 regression)
# ---------------------------------------------------------------------------


class TestExistingBehaviorPreserved(unittest.TestCase):
    def test_well_formed_custom_campaign_still_generates_successfully(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = Lead(
            first_name="Jane", last_name="Doe", full_name="Jane Doe",
            company_name="Acme Corp", job_title="Managing Partner",
            location="Austin, Texas, United States", industry="accounting",
            email="jane.doe@acme.com", email_status="validated",
            pipeline_status=PipelineStatus.EMAIL_VALIDATED.value,
        )
        store.upsert_lead(lead)
        campaign = create_campaign(
            "Q3 Outreach",
            "Quick question, {{first_name}}",
            (
                "Hi {{first_name}} {{last_name}},\n\n"
                "I noticed {{company_name}} and your work as {{job_title}} in "
                "{{location}} ({{industry}}). Would love to connect.\n\n"
                "Best,\nAlex"
            ),
        )
        updated = generate_email_for_lead(store, lead, campaign)
        self.assertEqual(updated.status, PipelineStatus.EMAIL_GENERATED)
        job = store.get_email_job(lead.lead_id)
        self.assertIn("Acme Corp", job["body"])
        self.assertIn("Managing Partner", job["body"])

    def test_format_industry_list_and_primary_term(self):
        self.assertEqual(format_industry_list("SaaS; AI; Automation"), "SaaS, AI, and Automation")
        self.assertEqual(format_industry_list("SaaS"), "SaaS")
        self.assertEqual(format_industry_list(""), "")
        self.assertEqual(primary_industry_term("SaaS; AI; Automation"), "SaaS")


if __name__ == "__main__":
    unittest.main()
