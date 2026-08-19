"""Regression tests for the email-discovery recall improvements: recovering
a clean, evidence-grounded company (and domain) for leads whose
company_name field is corrupted, generic, or blank, WITHOUT ever guessing
a domain from a name that isn't actually a company.

Background (see the diagnostic run against the real saas_test2.db leads):
of the 11/15 qualified SaaS-campaign leads with no email, most had either
a blank company_name (no recoverable evidence at all -- out of scope for
an offline fix) or a company_name corrupted by an upstream extraction bug
that glued the lead's own name/location onto the real company (e.g.
"Hebbia George Sivulka" for George Sivulka, actually Founder & CEO of
"Hebbia"). Two leads ALSO had "found" emails built on a bogus domain
guessed from a bare generic term (company_name="AI" -> ai.com,
company_name="RIT" -> rit.com is a university, not an employer) -- a
false "success" that looked fine in the funnel count but was precision,
not recall.

This file tests the fix in pipeline/quality.py (is_domain_guessable_company_name,
extract_domain_from_text, extract_company_from_dash_pattern) and
pipeline/email_discovery.py (ScrapegraphPatternGenerator._resolve_domains).

Run with (from the `scripts/` directory):
    python -m unittest pipeline.test_email_discovery_recall -v

No network. No real domains are contacted -- NullMXChecker/no MX checker
is used throughout.
"""

from __future__ import annotations

import unittest

from pipeline.email_discovery import ScrapegraphPatternGenerator, VENDOR_EMAIL_FINDER_AVAILABLE
from pipeline.models import Lead
from pipeline.quality import (
    extract_company_from_dash_pattern,
    extract_domain_from_text,
    is_domain_guessable_company_name,
)


# ---------------------------------------------------------------------------
# 1. is_domain_guessable_company_name
# ---------------------------------------------------------------------------


class TestIsDomainGuessableCompanyName(unittest.TestCase):
    def test_generic_bare_term_rejected(self):
        self.assertFalse(is_domain_guessable_company_name("AI"))
        self.assertFalse(is_domain_guessable_company_name("SaaS"))

    def test_generic_phrase_with_no_distinctive_token_rejected(self):
        self.assertFalse(is_domain_guessable_company_name("SaaS Company"))

    def test_empty_rejected(self):
        self.assertFalse(is_domain_guessable_company_name(""))

    def test_university_rejected(self):
        self.assertFalse(is_domain_guessable_company_name("Rochester Institute of Technology"))

    def test_distinctive_name_accepted(self):
        self.assertTrue(is_domain_guessable_company_name("Hebbia"))
        self.assertTrue(is_domain_guessable_company_name("SaasRise"))
        self.assertTrue(is_domain_guessable_company_name("EzData"))

    def test_distinctive_name_with_generic_word_still_accepted(self):
        # "AI" alone is generic; "Acme AI" is a real-looking distinct name.
        self.assertTrue(is_domain_guessable_company_name("Acme AI"))


# ---------------------------------------------------------------------------
# 2. extract_domain_from_text -- ground-truth domain evidence in free text
# ---------------------------------------------------------------------------


class TestExtractDomainFromText(unittest.TestCase):
    def test_finds_domain_in_parenthetical_url(self):
        text = "I'm building SaasRise (www.saasrise.com), a mastermind community"
        self.assertEqual(extract_domain_from_text(text), "saasrise.com")

    def test_no_domain_returns_empty(self):
        self.assertEqual(extract_domain_from_text("Founder and CEO of SaaS Company"), "")

    def test_social_and_mail_domains_are_skipped(self):
        text = "Connect with me on linkedin.com/in/janedoe or email jane@gmail.com"
        self.assertEqual(extract_domain_from_text(text), "")

    def test_finds_ai_tld_domain(self):
        self.assertEqual(extract_domain_from_text("check out ezdata.ai for more"), "ezdata.ai")


# ---------------------------------------------------------------------------
# 3. extract_company_from_dash_pattern -- "<Title> - <Company>" headlines
# ---------------------------------------------------------------------------


class TestExtractCompanyFromDashPattern(unittest.TestCase):
    def test_extracts_company_after_title_dash(self):
        self.assertEqual(
            extract_company_from_dash_pattern("Founder & CEO - Makesbridge"), "Makesbridge"
        )

    def test_extracts_multiword_company(self):
        self.assertEqual(
            extract_company_from_dash_pattern("Co-Founder & CGO - Mathos AI"), "Mathos AI"
        )

    def test_does_not_fire_without_a_title_cue_word(self):
        # "Shelly Freeman - CEO | COO | ..." -- the part before the first
        # dash is a name, not a title, so nothing should be extracted.
        self.assertEqual(
            extract_company_from_dash_pattern("Shelly Freeman - CEO | COO | Customer Success"), ""
        )

    def test_glued_name_company_headline_only_recovers_company_not_name(self):
        # A 3-segment "<Name> - <Company> - <Descriptor>" headline: the
        # first segment isn't a title, so this deliberately does NOT fire
        # (extract_company_from_at_pattern / job-title cleaning elsewhere
        # handle that shape) -- this function's job is narrowly the
        # 2-segment "<Title> - <Company>" case.
        self.assertEqual(
            extract_company_from_dash_pattern(
                "Jatin kumar - Hashbyt - AI-First Leading Frontend & UI/UX SaaS Partner"
            ),
            "",
        )

    def test_empty_input(self):
        self.assertEqual(extract_company_from_dash_pattern(""), "")


# ---------------------------------------------------------------------------
# 4. ScrapegraphPatternGenerator._resolve_domains -- end-to-end recovery
# ---------------------------------------------------------------------------


@unittest.skipUnless(VENDOR_EMAIL_FINDER_AVAILABLE, "vendored email finder not available")
class TestResolveDomainsRecovery(unittest.TestCase):
    def setUp(self):
        self.gen = ScrapegraphPatternGenerator()

    def test_glued_name_in_company_name_recovers_clean_company_via_job_title(self):
        # The exact real shape of the George Sivulka bug: company_name has
        # the lead's own name glued onto the real company.
        lead = Lead(
            first_name="George", last_name="Sivulka", full_name="George Sivulka",
            job_title="Founder & CEO at Hebbia",
            company_name="Hebbia George Sivulka",
            profile_summary="George Sivulka is the Founder & CEO of Hebbia, an AI-powered knowledge graph SaaS platform.",
        )
        domains, guessed = self.gen._resolve_domains(lead)
        self.assertTrue(guessed)
        self.assertIn("hebbia.com", domains)
        self.assertNotIn("hebbiageorgesivulka.com", domains)

    def test_glued_name_and_location_recovers_clean_company(self):
        # The exact real shape of the Ryan Allis bug, PLUS a literal domain
        # mentioned in the summary -- the domain-in-text path should win
        # outright rather than falling through to a guess at all.
        lead = Lead(
            first_name="Ryan", last_name="Allis", full_name="Ryan Allis",
            job_title="Founder at SaasRise",
            company_name="SaasRise Ryan Allis. Austin",
            profile_summary="I'm building SaasRise (www.saasrise.com), a mastermind community for software CEOs.",
        )
        domains, guessed = self.gen._resolve_domains(lead)
        self.assertEqual(domains, ["saasrise.com"])
        self.assertFalse(guessed)  # read directly from text, not a slug guess

    def test_blank_company_name_recovered_from_dash_pattern_job_title(self):
        # The exact real shape of the Jay Adams / YJ Guo cases: no
        # company_name at all, but job_title carries it in "<Title> -
        # <Company>" form.
        lead = Lead(
            first_name="Jay", last_name="Adams", full_name="Jay Adams",
            job_title="Founder & CEO - Makesbridge", company_name="",
        )
        domains, guessed = self.gen._resolve_domains(lead)
        self.assertTrue(guessed)
        self.assertIn("makesbridge.com", domains)

    def test_generic_company_name_never_produces_a_domain_guess(self):
        # The exact real shape of the MCHAEL / Charles Frost false-positive
        # bug: a generic term/phrase must never be slugified into a domain,
        # even though it's technically well-formed text.
        lead = Lead(first_name="Pat", last_name="Doe", job_title="CEO", company_name="AI")
        domains, guessed = self.gen._resolve_domains(lead)
        self.assertEqual(domains, [])

        lead2 = Lead(
            first_name="Pat", last_name="Doe",
            job_title="Founder and CEO of SaaS Company", company_name="SaaS Company",
        )
        domains2, guessed2 = self.gen._resolve_domains(lead2)
        self.assertEqual(domains2, [])

    def test_university_company_name_never_produces_a_domain_guess(self):
        lead = Lead(
            first_name="Adib", last_name="Ahnaf", job_title="Founder/COO",
            company_name="Rochester Institute of Technology",
        )
        domains, guessed = self.gen._resolve_domains(lead)
        self.assertEqual(domains, [])

    def test_explicit_company_domain_still_wins_over_everything(self):
        lead = Lead(
            first_name="Jane", last_name="Doe", company_domain="acme.com",
            company_name="Totally Different Name", job_title="Founder at SomethingElse",
        )
        domains, guessed = self.gen._resolve_domains(lead)
        self.assertEqual(domains, ["acme.com"])
        self.assertFalse(guessed)

    def test_valid_specific_company_name_unaffected_by_recovery_logic(self):
        # A normal, already-clean company_name should pass straight
        # through exactly as before -- recovery must not "fix" something
        # that wasn't broken.
        lead = Lead(
            first_name="Krishna", last_name="Parimi", job_title="CEO",
            company_name="EzData",
            profile_summary="CEO leading EzData's AI-native solutions.",
        )
        domains, guessed = self.gen._resolve_domains(lead)
        self.assertTrue(guessed)
        self.assertTrue(any(d.startswith("ezdata.") for d in domains))

    def test_end_to_end_generate_produces_candidates_for_recovered_company(self):
        lead = Lead(
            first_name="George", last_name="Sivulka", full_name="George Sivulka",
            job_title="Founder & CEO at Hebbia", company_name="Hebbia George Sivulka",
        )
        candidates = self.gen.generate(lead)
        self.assertTrue(candidates)
        self.assertTrue(any(c.domain == "hebbia.com" for c in candidates))
        self.assertTrue(all("hebbiageorgesivulka" not in c.domain for c in candidates))

    def test_end_to_end_generate_produces_nothing_for_generic_company(self):
        lead = Lead(first_name="Pat", last_name="Doe", job_title="CEO", company_name="AI")
        candidates = self.gen.generate(lead)
        self.assertEqual(candidates, [])


if __name__ == "__main__":
    unittest.main()
