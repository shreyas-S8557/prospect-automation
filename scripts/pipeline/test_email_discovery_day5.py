"""Day 5 milestone tests: email-discovery candidate pipeline + the
QUALIFIED -> EMAIL_CANDIDATES_FOUND / EMAIL_NOT_FOUND stage.

Run with (from the `scripts/` directory, or as a module from repo root):
    python -m unittest pipeline.test_email_discovery_day5 -v

Does NOT hit the network or spawn Node: every test that needs an MX/SMTP
checker or an optional generator (Mailfoguess / email-finder-main) injects a
fake one, so results are deterministic and fast regardless of what's
installed on the machine running the suite. The one exception is a single,
explicitly-skippable smoke test that exercises the real NodeMXChecker
end-to-end when Node happens to be available (see TestRealNodeIntegration).

Covers (per the Day 5 spec):
  - candidate generation (primary generator, from Lead fields)
  - candidate normalization
  - duplicate candidates (cross-generator dedup)
  - candidate ranking / scoring
  - domain/MX handling
  - validation states (usable vs. disqualified)
  - EMAIL_CANDIDATES_FOUND
  - EMAIL_NOT_FOUND
  - persistence / resume
  - Day 2-4 regression
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
import unittest.mock
from pathlib import Path

from .email_discovery import (
    EmailCandidate,
    EmailFinderMainGenerator,
    MailfoguessGenerator,
    NodeMXChecker,
    ScrapegraphPatternGenerator,
    confidence_label,
    dedupe_candidates,
    find_and_score_pending_leads,
    generate_candidates_for_lead,
    is_usable,
    normalize_email,
    process_lead_email,
    rank_candidates,
    score_candidate,
    MX_DEAD,
    MX_UNKNOWN,
    MX_VALID,
    SMTP_CATCH_ALL,
    SMTP_EXISTS,
    SMTP_NOT_CHECKED,
    SMTP_NOT_EXISTS,
    SMTP_UNKNOWN,
)
from .lead_pipeline import ingest_discovery_rows, normalize_investor_row, qualify_pending_leads
from .lead_store import LeadStore
from .models import InvalidStateTransition, Lead, PipelineStatus, validate_transition
from .quality import matches_target_criteria
from .query_generator import build_queries
from .target_config import CPA_PARTNER_PRESET, TargetConfig


def make_lead(**overrides) -> Lead:
    defaults = dict(
        first_name="Jane",
        last_name="Doe",
        full_name="Jane Doe",
        company_name="Acme Corp",
        pipeline_status=PipelineStatus.QUALIFIED.value,
    )
    defaults.update(overrides)
    return Lead(**defaults)


class FakeMXChecker:
    """Deterministic MX checker for tests: domain -> status map, default UNKNOWN."""

    def __init__(self, statuses: dict[str, str] | None = None):
        self.statuses = statuses or {}
        self.calls: list[list[str]] = []

    def check_domains(self, domains):
        self.calls.append(list(domains))
        return {d: self.statuses.get(d, MX_UNKNOWN) for d in domains}


class FakeSMTPChecker:
    """Deterministic SMTP checker for tests: email -> status map, default UNKNOWN."""

    def __init__(self, statuses: dict[str, str] | None = None):
        self.statuses = statuses or {}
        self.calls: list[list[str]] = []

    def check_emails(self, emails):
        self.calls.append(list(emails))
        return {e: self.statuses.get(e, SMTP_UNKNOWN) for e in emails}


class StubGenerator:
    """A CandidateGenerator stand-in that returns a fixed candidate list,
    for tests that need to control exactly what generators contribute
    without depending on the pattern-guessing algorithm."""

    def __init__(self, name: str, candidates: list[EmailCandidate], available: bool = True):
        self.name = name
        self._candidates = candidates
        self.available = available

    def generate(self, lead):
        return list(self._candidates)


# ---------------------------------------------------------------------------
# 1. Candidate generation
# ---------------------------------------------------------------------------


class TestCandidateGeneration(unittest.TestCase):
    def test_generates_candidates_from_name_and_company(self):
        gen = ScrapegraphPatternGenerator()
        lead = make_lead(first_name="Jane", last_name="Doe", company_name="Acme Corp")
        candidates = gen.generate(lead)
        emails = {c.email for c in candidates}
        self.assertIn("jane.doe@acme.com", emails)
        self.assertIn("janedoe@acme.com", emails)
        self.assertTrue(all(c.sources == ("scrapegraph_pattern",) for c in candidates))

    def test_uses_explicit_company_domain_over_guessed_one(self):
        gen = ScrapegraphPatternGenerator()
        lead = make_lead(company_name="Acme Corp LLC", company_domain="acme-real-site.io")
        candidates = gen.generate(lead)
        self.assertTrue(all(c.domain == "acme-real-site.io" for c in candidates))
        self.assertTrue(all(c.domain_guessed is False for c in candidates))

    def test_falls_back_to_slugified_guess_without_explicit_domain(self):
        gen = ScrapegraphPatternGenerator()
        lead = make_lead(company_name="Smith & Jones CPAs LLP", company_domain="")
        candidates = gen.generate(lead)
        self.assertTrue(candidates)
        self.assertTrue(all(c.domain_guessed for c in candidates))
        # No single TLD is committed to for a guessed domain — AI/SaaS
        # companies are as often on .ai/.io/.co as .com, so all four are
        # tried and left for MX checking to disambiguate (see
        # ScrapegraphPatternGenerator._GUESS_TLDS).
        domains_seen = {c.domain for c in candidates}
        self.assertEqual(
            domains_seen,
            {"smithjones.com", "smithjones.ai", "smithjones.io", "smithjones.co"},
        )

    def test_no_candidates_without_first_name(self):
        gen = ScrapegraphPatternGenerator()
        lead = make_lead(first_name="", last_name="", full_name="", company_name="Acme Corp")
        self.assertEqual(gen.generate(lead), [])

    def test_no_candidates_without_resolvable_company(self):
        gen = ScrapegraphPatternGenerator()
        lead = make_lead(company_name="", company_domain="")
        self.assertEqual(gen.generate(lead), [])

    def test_derives_names_from_full_name_when_first_last_blank(self):
        gen = ScrapegraphPatternGenerator()
        lead = make_lead(first_name="", last_name="", full_name="Jane Doe", company_name="Acme Corp")
        candidates = gen.generate(lead)
        self.assertIn("jane.doe@acme.com", {c.email for c in candidates})

    def test_optional_generators_are_soft_dependencies(self):
        # Neither Mailfoguess nor email-finder-main ships in this repo —
        # both must degrade to "no candidates", never raise.
        mailfoguess = MailfoguessGenerator()
        fallback = EmailFinderMainGenerator()
        self.assertFalse(mailfoguess.available)
        self.assertFalse(fallback.available)
        self.assertEqual(mailfoguess.generate(make_lead()), [])
        self.assertEqual(fallback.generate(make_lead()), [])

    def test_full_orchestration_uses_primary_generator_end_to_end(self):
        lead = make_lead()
        result = generate_candidates_for_lead(
            lead, mx_checker=FakeMXChecker(), smtp_checker=FakeSMTPChecker()
        )
        self.assertTrue(result.candidates)
        self.assertIsNotNone(result.best)
        self.assertEqual(result.best.source, "scrapegraph_pattern")
        self.assertFalse(result.used_fallback)

    def test_fallback_generator_only_used_when_others_find_nothing(self):
        lead = make_lead(first_name="", last_name="", full_name="", company_name="")
        fallback_candidates = [EmailCandidate(email="jane@fallback.com", sources=("email_finder_main",), domain="fallback.com")]
        result = generate_candidates_for_lead(
            lead,
            primary_generators=[StubGenerator("scrapegraph_pattern", [])],
            supplementary_generators=[StubGenerator("mailfoguess", [])],
            fallback_generator=StubGenerator("email_finder_main", fallback_candidates),
            mx_checker=FakeMXChecker(),
            smtp_checker=FakeSMTPChecker(),
        )
        self.assertTrue(result.used_fallback)
        self.assertEqual(result.best.email, "jane@fallback.com")

    def test_fallback_not_used_when_primary_already_found_candidates(self):
        lead = make_lead()
        fallback_stub = StubGenerator(
            "email_finder_main", [EmailCandidate(email="should-not-appear@fallback.com", sources=("email_finder_main",))]
        )
        result = generate_candidates_for_lead(
            lead, fallback_generator=fallback_stub, mx_checker=FakeMXChecker(), smtp_checker=FakeSMTPChecker()
        )
        self.assertFalse(result.used_fallback)
        emails = {c.email for c in result.candidates}
        self.assertNotIn("should-not-appear@fallback.com", emails)


# ---------------------------------------------------------------------------
# 2. Candidate normalization
# ---------------------------------------------------------------------------


class TestCandidateNormalization(unittest.TestCase):
    def test_lowercases_and_strips(self):
        self.assertEqual(normalize_email("  Jane.Doe@ACME.com  "), "jane.doe@acme.com")

    def test_strips_angle_brackets(self):
        self.assertEqual(normalize_email("<jane.doe@acme.com>"), "jane.doe@acme.com")

    def test_rejects_malformed_addresses(self):
        for bad in ["", "not-an-email", "jane@", "@acme.com", "jane at acme.com", None]:
            self.assertEqual(normalize_email(bad), "")

    def test_rejects_placeholder_domains(self):
        for bad in ["jane@example.com", "jane@domain.com", "jane@yourcompany.com"]:
            self.assertEqual(normalize_email(bad), "")

    def test_accepts_plausible_business_address(self):
        self.assertEqual(normalize_email("j.doe@acme-consulting.io"), "j.doe@acme-consulting.io")


# ---------------------------------------------------------------------------
# 3. Duplicate candidates
# ---------------------------------------------------------------------------


class TestDuplicateCandidates(unittest.TestCase):
    def test_dedupe_merges_same_email_across_sources(self):
        candidates = [
            EmailCandidate(email="jane.doe@acme.com", sources=("scrapegraph_pattern",), patterns=("{first}.{last}",), domain="acme.com"),
            EmailCandidate(email="jane.doe@acme.com", sources=("mailfoguess",), domain="acme.com"),
        ]
        merged = dedupe_candidates(candidates)
        self.assertEqual(len(merged), 1)
        self.assertEqual(set(merged[0].sources), {"scrapegraph_pattern", "mailfoguess"})

    def test_dedupe_preserves_distinct_emails(self):
        candidates = [
            EmailCandidate(email="jane.doe@acme.com", sources=("scrapegraph_pattern",)),
            EmailCandidate(email="jdoe@acme.com", sources=("scrapegraph_pattern",)),
        ]
        merged = dedupe_candidates(candidates)
        self.assertEqual(len(merged), 2)

    def test_dedupe_drops_blank_emails(self):
        candidates = [EmailCandidate(email=""), EmailCandidate(email="jane@acme.com")]
        merged = dedupe_candidates(candidates)
        self.assertEqual(len(merged), 1)

    def test_generate_candidates_for_lead_dedupes_across_generators(self):
        shared = EmailCandidate(email="jane.doe@acme.com", sources=("mailfoguess",), domain="acme.com")
        result = generate_candidates_for_lead(
            make_lead(),
            supplementary_generators=[StubGenerator("mailfoguess", [shared])],
            mx_checker=FakeMXChecker(),
            smtp_checker=FakeSMTPChecker(),
        )
        matches = [c for c in result.candidates if c.email == "jane.doe@acme.com"]
        self.assertEqual(len(matches), 1)
        self.assertIn("mailfoguess", matches[0].sources)
        self.assertIn("scrapegraph_pattern", matches[0].sources)


# ---------------------------------------------------------------------------
# 4. Candidate ranking
# ---------------------------------------------------------------------------


class TestCandidateRanking(unittest.TestCase):
    def test_higher_score_ranks_first(self):
        low = EmailCandidate(email="a@x.com", score=0.3)
        high = EmailCandidate(email="b@x.com", score=0.9)
        ranked = rank_candidates([low, high])
        self.assertEqual([c.email for c in ranked], ["b@x.com", "a@x.com"])

    def test_multi_source_agreement_boosts_score(self):
        single = EmailCandidate(email="a@x.com", sources=("scrapegraph_pattern",), score=0.5)
        multi = EmailCandidate(email="b@x.com", sources=("scrapegraph_pattern", "mailfoguess"), score=0.5)
        self.assertGreater(score_candidate(multi), score_candidate(single))

    def test_valid_mx_increases_score_dead_mx_disqualifies(self):
        valid = EmailCandidate(email="a@x.com", score=0.5, mx_status=MX_VALID)
        unknown = EmailCandidate(email="b@x.com", score=0.5, mx_status=MX_UNKNOWN)
        dead = EmailCandidate(email="c@x.com", score=0.5, mx_status=MX_DEAD)
        self.assertGreater(score_candidate(valid), score_candidate(unknown))
        self.assertFalse(is_usable(dead))
        self.assertTrue(is_usable(valid))
        self.assertTrue(is_usable(unknown))

    def test_smtp_exists_boosts_not_exists_disqualifies(self):
        exists = EmailCandidate(email="a@x.com", score=0.5, smtp_status=SMTP_EXISTS)
        not_exists = EmailCandidate(email="b@x.com", score=0.5, smtp_status=SMTP_NOT_EXISTS)
        unknown = EmailCandidate(email="c@x.com", score=0.5, smtp_status=SMTP_UNKNOWN)
        self.assertGreater(score_candidate(exists), score_candidate(unknown))
        self.assertFalse(is_usable(not_exists))
        self.assertTrue(is_usable(exists))

    def test_smtp_is_supporting_evidence_never_absolute_proof(self):
        # A candidate with SMTP EXISTS is boosted but not automatically
        # maxed out to 1.0/"proven" — score is nudged, not overridden.
        c = EmailCandidate(email="a@x.com", score=0.5, smtp_status=SMTP_EXISTS)
        self.assertLess(score_candidate(c), 1.0)

    def test_confidence_labels(self):
        self.assertEqual(confidence_label(EmailCandidate(email="a@x.com", score=0.9)), "high")
        self.assertEqual(confidence_label(EmailCandidate(email="a@x.com", score=0.6)), "medium")
        self.assertEqual(confidence_label(EmailCandidate(email="a@x.com", score=0.1)), "low")
        self.assertEqual(
            confidence_label(EmailCandidate(email="a@x.com", score=0.9, mx_status=MX_DEAD)), "none"
        )

    def test_best_candidate_is_highest_ranked_usable_one(self):
        result = generate_candidates_for_lead(
            make_lead(),
            mx_checker=FakeMXChecker({"acme.com": MX_VALID}),
            smtp_checker=FakeSMTPChecker(),
        )
        self.assertEqual(result.best, result.candidates[0])
        self.assertEqual(result.best.email, "jane.doe@acme.com")  # {first}.{last} is the top pattern


# ---------------------------------------------------------------------------
# 5. Domain / MX handling
# ---------------------------------------------------------------------------


class TestDomainMXHandling(unittest.TestCase):
    def test_mx_checker_invoked_once_per_unique_domain(self):
        mx = FakeMXChecker({"acme.com": MX_VALID})
        generate_candidates_for_lead(make_lead(), mx_checker=mx, smtp_checker=FakeSMTPChecker())
        # One batched call, covering every unique guessed-TLD domain (see
        # ScrapegraphPatternGenerator._GUESS_TLDS) — not one call per domain.
        self.assertEqual(len(mx.calls), 1)
        self.assertEqual(sorted(mx.calls[0]), ["acme.ai", "acme.co", "acme.com", "acme.io"])

    def test_dead_domain_excludes_all_its_candidates_from_usable(self):
        result = generate_candidates_for_lead(
            make_lead(),
            mx_checker=FakeMXChecker(
                {"acme.com": MX_DEAD, "acme.ai": MX_DEAD, "acme.io": MX_DEAD, "acme.co": MX_DEAD}
            ),
            smtp_checker=FakeSMTPChecker(),
        )
        self.assertIsNone(result.best)
        self.assertTrue(result.candidates)  # still generated/ranked, just none usable
        self.assertTrue(all(not is_usable(c) for c in result.candidates))

    def test_correct_non_com_tld_wins_over_default_com_guess(self):
        # The real point of trying multiple TLDs: a startup whose actual
        # domain is .ai (extremely common for this project's AI/SaaS ICP)
        # must not lose to a dead .com just because .com is guessed first.
        result = generate_candidates_for_lead(
            make_lead(),
            mx_checker=FakeMXChecker({"acme.com": MX_DEAD, "acme.ai": MX_VALID}),
            smtp_checker=FakeSMTPChecker(),
        )
        self.assertIsNotNone(result.best)
        self.assertEqual(result.best.domain, "acme.ai")
        self.assertTrue(result.best.email.endswith("@acme.ai"))

    def test_mx_checker_unavailable_yields_unknown_not_dead(self):
        # Item 5: "where already supported" — missing Node/script must
        # degrade to UNKNOWN, never silently read as DEAD.
        checker = NodeMXChecker(script_path=Path("/does/not/exist.js"))
        self.assertFalse(checker.available)
        self.assertEqual(checker.check_domains(["acme.com"]), {"acme.com": MX_UNKNOWN})

    def test_resolved_domain_is_returned_for_backfilling_company_domain(self):
        result = generate_candidates_for_lead(
            make_lead(company_domain=""), mx_checker=FakeMXChecker(), smtp_checker=FakeSMTPChecker()
        )
        self.assertEqual(result.resolved_domain, "acme.com")


# ---------------------------------------------------------------------------
# 6. Validation states (SMTP as supporting evidence only)
# ---------------------------------------------------------------------------


class TestValidationStates(unittest.TestCase):
    def test_smtp_not_checked_by_default(self):
        result = generate_candidates_for_lead(
            make_lead(), mx_checker=FakeMXChecker(), smtp_checker=FakeSMTPChecker({"jane.doe@acme.com": SMTP_EXISTS})
        )
        # enable_smtp defaults to False -> SMTP checker must not even be consulted.
        self.assertTrue(all(c.smtp_status == SMTP_NOT_CHECKED for c in result.candidates))

    def test_smtp_checked_when_explicitly_enabled(self):
        smtp = FakeSMTPChecker({"jane.doe@acme.com": SMTP_EXISTS})
        result = generate_candidates_for_lead(
            make_lead(), mx_checker=FakeMXChecker(), smtp_checker=smtp, enable_smtp=True
        )
        best = next(c for c in result.candidates if c.email == "jane.doe@acme.com")
        self.assertEqual(best.smtp_status, SMTP_EXISTS)
        self.assertTrue(smtp.calls)

    def test_smtp_skipped_for_already_dead_domains(self):
        smtp = FakeSMTPChecker({"jane.doe@acme.com": SMTP_EXISTS})
        generate_candidates_for_lead(
            make_lead(),
            mx_checker=FakeMXChecker(
                {"acme.com": MX_DEAD, "acme.ai": MX_DEAD, "acme.io": MX_DEAD, "acme.co": MX_DEAD}
            ),
            smtp_checker=smtp,
            enable_smtp=True,
        )
        # No point SMTP-probing a domain whose MX is already known dead —
        # the checker must not be invoked at all.
        self.assertEqual(smtp.calls, [])

    def test_catch_all_is_weak_positive_not_confirmation(self):
        catch_all = EmailCandidate(email="a@x.com", score=0.5, smtp_status=SMTP_CATCH_ALL)
        confirmed = EmailCandidate(email="b@x.com", score=0.5, smtp_status=SMTP_EXISTS)
        self.assertLess(score_candidate(catch_all), score_candidate(confirmed))
        self.assertTrue(is_usable(catch_all))


# ---------------------------------------------------------------------------
# 7. EMAIL_CANDIDATES_FOUND
# ---------------------------------------------------------------------------


class TestEmailCandidatesFound(unittest.TestCase):
    def test_qualified_lead_with_usable_candidate_transitions_and_populates_fields(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)

        updated = process_lead_email(store, lead, mx_checker=FakeMXChecker({"acme.com": MX_VALID}), smtp_checker=FakeSMTPChecker())

        self.assertEqual(updated.status, PipelineStatus.EMAIL_CANDIDATES_FOUND)
        self.assertEqual(updated.email, "jane.doe@acme.com")
        self.assertEqual(updated.email_source, "scrapegraph_pattern")
        self.assertEqual(updated.email_status, "candidates_found")
        self.assertIn(updated.email_confidence, ("high", "medium", "low"))

    def test_backfills_company_domain_when_previously_blank(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead(company_domain="")
        store.upsert_lead(lead)
        updated = process_lead_email(store, lead, mx_checker=FakeMXChecker(), smtp_checker=FakeSMTPChecker())
        self.assertEqual(updated.company_domain, "acme.com")

    def test_does_not_overwrite_existing_company_domain(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead(company_domain="already-known.com")
        store.upsert_lead(lead)
        updated = process_lead_email(store, lead, mx_checker=FakeMXChecker(), smtp_checker=FakeSMTPChecker())
        self.assertEqual(updated.company_domain, "already-known.com")


# ---------------------------------------------------------------------------
# 8. EMAIL_NOT_FOUND
# ---------------------------------------------------------------------------


class TestEmailNotFound(unittest.TestCase):
    def test_no_candidates_transitions_to_email_not_found(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead(first_name="", last_name="", full_name="", company_name="")
        store.upsert_lead(lead)
        updated = process_lead_email(
            store, lead,
            supplementary_generators=[],
            fallback_generator=StubGenerator("email_finder_main", [], available=True),
            mx_checker=FakeMXChecker(), smtp_checker=FakeSMTPChecker(),
        )
        self.assertEqual(updated.status, PipelineStatus.EMAIL_NOT_FOUND)
        self.assertEqual(updated.email, "")
        self.assertEqual(updated.email_status, "not_found")
        self.assertEqual(updated.email_confidence, "none")

    def test_all_candidates_disqualified_by_dead_mx_transitions_to_not_found(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        updated = process_lead_email(
            store,
            lead,
            mx_checker=FakeMXChecker(
                {"acme.com": MX_DEAD, "acme.ai": MX_DEAD, "acme.io": MX_DEAD, "acme.co": MX_DEAD}
            ),
            smtp_checker=FakeSMTPChecker(),
        )
        self.assertEqual(updated.status, PipelineStatus.EMAIL_NOT_FOUND)

    def test_email_candidates_found_and_not_found_are_terminal_for_this_stage(self):
        with self.assertRaises(InvalidStateTransition):
            validate_transition(PipelineStatus.EMAIL_NOT_FOUND, PipelineStatus.EMAIL_VALIDATED)
        # EMAIL_CANDIDATES_FOUND legally continues on to EMAIL_VALIDATED
        # (Day 6+, not implemented here) or EMAIL_NOT_FOUND is illegal from there:
        self.assertEqual(
            validate_transition(PipelineStatus.EMAIL_CANDIDATES_FOUND, PipelineStatus.EMAIL_VALIDATED),
            PipelineStatus.EMAIL_VALIDATED,
        )


# ---------------------------------------------------------------------------
# 9. Persistence / resume
# ---------------------------------------------------------------------------


class TestPersistenceAndResume(unittest.TestCase):
    def test_find_and_score_pending_only_touches_qualified_leads(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        found_lead = make_lead(linkedin_url="https://linkedin.com/in/found")
        not_found_lead = make_lead(
            first_name="", last_name="", full_name="", company_name="",
            linkedin_url="https://linkedin.com/in/notfound",
        )
        store.upsert_lead(found_lead)
        store.upsert_lead(not_found_lead)

        stats1 = find_and_score_pending_leads(
            store, mx_checker=FakeMXChecker({"acme.com": MX_VALID}), smtp_checker=FakeSMTPChecker()
        )
        self.assertEqual(stats1, {"email_candidates_found": 1, "email_not_found": 1})
        self.assertEqual(len(store.list_by_status(PipelineStatus.QUALIFIED)), 0)

        # "Resume": calling again must be a no-op — nothing left QUALIFIED.
        stats2 = find_and_score_pending_leads(store, mx_checker=FakeMXChecker(), smtp_checker=FakeSMTPChecker())
        self.assertEqual(stats2, {"email_candidates_found": 0, "email_not_found": 0})

    def test_resumed_run_does_not_reprocess_already_advanced_lead(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        find_and_score_pending_leads(store, mx_checker=FakeMXChecker({"acme.com": MX_VALID}), smtp_checker=FakeSMTPChecker())
        [advanced] = store.all()
        self.assertEqual(advanced.status, PipelineStatus.EMAIL_CANDIDATES_FOUND)

        # Simulate a second run of the same stage after a restart.
        stats = find_and_score_pending_leads(store, mx_checker=FakeMXChecker(), smtp_checker=FakeSMTPChecker())
        self.assertEqual(stats, {"email_candidates_found": 0, "email_not_found": 0})
        [still_one] = store.all()
        self.assertEqual(still_one.lead_id, advanced.lead_id)
        self.assertEqual(still_one.status, PipelineStatus.EMAIL_CANDIDATES_FOUND)

    def test_fields_persist_across_a_reload_of_the_same_sqlite_file(self):
        db_path = Path("/tmp/day5_email_discovery_test.db")
        db_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        try:
            store1 = LeadStore(db_path)
            self.addCleanup(store1.close)
            lead = make_lead()
            store1.upsert_lead(lead)
            process_lead_email(store1, lead, mx_checker=FakeMXChecker({"acme.com": MX_VALID}), smtp_checker=FakeSMTPChecker())
            store1.close()

            store2 = LeadStore(db_path)
            self.addCleanup(store2.close)
            reloaded = store2.get(lead.lead_id)
            self.assertEqual(reloaded.status, PipelineStatus.EMAIL_CANDIDATES_FOUND)
            self.assertEqual(reloaded.email, "jane.doe@acme.com")
            self.assertEqual(reloaded.email_confidence, "high")
            store2.close()
        finally:
            db_path.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(db_path) + suffix).unlink(missing_ok=True)

    def test_save_does_not_change_pipeline_status(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        lead.email = "jane.doe@acme.com"
        store.save(lead)
        reloaded = store.get(lead.lead_id)
        self.assertEqual(reloaded.email, "jane.doe@acme.com")
        self.assertEqual(reloaded.status, PipelineStatus.QUALIFIED)  # unchanged


# ---------------------------------------------------------------------------
# 10. Day 2-4 regression
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

    def test_lead_state_machine_unchanged(self):
        self.assertEqual(
            validate_transition(PipelineStatus.QUALIFIED, PipelineStatus.EMAIL_CANDIDATES_FOUND),
            PipelineStatus.EMAIL_CANDIDATES_FOUND,
        )
        with self.assertRaises(InvalidStateTransition):
            validate_transition(PipelineStatus.QUALIFIED, PipelineStatus.EMAIL_VALIDATED)


# ---------------------------------------------------------------------------
# Optional: real Node integration smoke test (skipped if Node is missing)
# ---------------------------------------------------------------------------


class TestNodeMXCheckerSchema(unittest.TestCase):
    """Unit tests for NodeMXChecker's Python-side JSON parsing, independent
    of Node actually running — verify_mx.js is mocked out entirely by
    writing the exact JSON it would produce.
    """

    def _checker_with_fake_node(self, output_obj):
        checker = NodeMXChecker(script_path=Path(__file__))  # any existing file, so .available is True
        checker._node = "true"  # POSIX no-op binary; subprocess.run succeeds trivially

        real_run = subprocess.run

        def fake_run(cmd, **kwargs):
            # cmd[-1] is the -o output path passed by check_domains()
            out_path = Path(cmd[-1])
            out_path.write_text(json.dumps(output_obj), encoding="utf-8")
            return real_run(["true"])

        return checker, fake_run

    def test_unknown_domains_are_not_read_as_dead(self):
        # This is the actual bug this schema change fixes: a domain whose
        # DNS lookup failed for an infrastructure reason (timeout, no
        # network, DNS server down) must come back MX_UNKNOWN, never
        # MX_DEAD -- the old flat-array format couldn't represent this
        # distinction at all, so every non-"valid" domain silently became
        # DEAD, which would disqualify every real candidate the moment DNS
        # is flaky, firewalled, or rate-limited.
        checker, fake_run = self._checker_with_fake_node(
            {"valid": ["probe@good.com"], "dead": ["probe@fake.com"], "unknown": ["probe@flaky.com"]}
        )
        with unittest.mock.patch("subprocess.run", side_effect=fake_run):
            result = checker.check_domains(["good.com", "fake.com", "flaky.com"])
        self.assertEqual(result["good.com"], MX_VALID)
        self.assertEqual(result["fake.com"], MX_DEAD)
        self.assertEqual(result["flaky.com"], MX_UNKNOWN)

    def test_old_flat_array_format_still_supported(self):
        # Backward compatibility with the pre-fix output shape, in case any
        # external tooling still produces it.
        checker, fake_run = self._checker_with_fake_node(["probe@good.com"])
        with unittest.mock.patch("subprocess.run", side_effect=fake_run):
            result = checker.check_domains(["good.com", "other.com"])
        self.assertEqual(result["good.com"], MX_VALID)
        self.assertEqual(result["other.com"], MX_DEAD)


class TestRealNodeIntegration(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js not available in this environment")
    def test_node_mx_checker_runs_without_raising(self):
        checker = NodeMXChecker(timeout=5)
        # Only asserts the call completes and returns a status for the
        # domain — not any particular verdict, since that depends on
        # whatever network access this environment has.
        result = checker.check_domains(["acme.com"])
        self.assertIn("acme.com", result)
        self.assertIn(result["acme.com"], (MX_VALID, MX_DEAD, MX_UNKNOWN))


if __name__ == "__main__":
    unittest.main()
