"""Day 6 milestone tests: candidate persistence + the
EMAIL_CANDIDATES_FOUND -> EMAIL_VALIDATED / EMAIL_NOT_FOUND stage.

Run with (from the `scripts/` directory, or as a module from repo root):
    python -m unittest pipeline.test_email_validation_day6 -v

Does NOT hit the network or spawn Node — every test that needs an MX/SMTP
checker injects a fake one, so results are deterministic and fast regardless
of what's installed on the machine running the suite.

Covers (per the Day 6 spec):
  - candidate persistence / reloading
  - candidate ranking (via persisted rows)
  - MX valid / dead / unknown
  - SMTP exists / not-exists / unknown
  - best-candidate selection (from storage, no regeneration)
  - invalid candidate rejection
  - resume after interruption
  - state transitions (QUALIFIED -> EMAIL_CANDIDATES_FOUND -> EMAIL_VALIDATED
    / EMAIL_NOT_FOUND)
  - Day 2-5 regression
"""

from __future__ import annotations

import unittest
from pathlib import Path

from .email_discovery import (
    MX_DEAD,
    MX_UNKNOWN,
    MX_VALID,
    SMTP_CATCH_ALL,
    SMTP_EXISTS,
    SMTP_NOT_CHECKED,
    SMTP_NOT_EXISTS,
    SMTP_UNKNOWN,
    VALIDATION_DOMAIN_VALID,
    VALIDATION_GENERATED,
    VALIDATION_INVALID,
    VALIDATION_SMTP_CONFIRMED,
    VALIDATION_SMTP_INCONCLUSIVE,
    VALIDATION_UNKNOWN,
    EmailCandidate,
    candidate_row_from_dict,
    candidate_to_row,
    candidates_to_rows,
    classify_validation_status,
    generate_candidates_for_lead,
    process_lead_email,
    select_best_row,
)
from .email_validation import (
    find_and_validate_pending_leads,
    load_candidate_rows,
    validate_and_select_email,
)
from .lead_pipeline import ingest_discovery_rows, qualify_pending_leads
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
    def __init__(self, statuses: dict[str, str] | None = None):
        self.statuses = statuses or {}
        self.calls: list[list[str]] = []

    def check_domains(self, domains):
        self.calls.append(list(domains))
        return {d: self.statuses.get(d, MX_UNKNOWN) for d in domains}


class FakeSMTPChecker:
    def __init__(self, statuses: dict[str, str] | None = None):
        self.statuses = statuses or {}
        self.calls: list[list[str]] = []

    def check_emails(self, emails):
        self.calls.append(list(emails))
        return {e: self.statuses.get(e, SMTP_UNKNOWN) for e in emails}


# ---------------------------------------------------------------------------
# 1. Candidate persistence / reloading
# ---------------------------------------------------------------------------


class TestCandidatePersistence(unittest.TestCase):
    def test_row_round_trips_through_to_row_and_from_dict(self):
        candidate = EmailCandidate(
            email="jane.doe@acme.com",
            sources=("scrapegraph_pattern", "mailfoguess"),
            patterns=("{first}.{last}",),
            domain="acme.com",
            domain_guessed=False,
            mx_status=MX_VALID,
            smtp_status=SMTP_EXISTS,
            mx_checked=True,
            smtp_checked=True,
            score=0.9,
        )
        row = candidate_to_row(candidate, lead_id="lead-1", rank=0, is_best=True)
        rebuilt = candidate_row_from_dict(row)
        self.assertEqual(rebuilt.email, "jane.doe@acme.com")
        self.assertEqual(set(rebuilt.sources), {"scrapegraph_pattern", "mailfoguess"})
        self.assertEqual(rebuilt.patterns, ("{first}.{last}",))
        self.assertEqual(rebuilt.domain, "acme.com")
        self.assertEqual(rebuilt.mx_status, MX_VALID)
        self.assertEqual(rebuilt.smtp_status, SMTP_EXISTS)
        self.assertTrue(rebuilt.is_best)
        self.assertEqual(rebuilt.rank, 0)
        self.assertEqual(rebuilt.validation_status, VALIDATION_SMTP_CONFIRMED)

    def test_save_and_list_candidates_round_trips_through_sqlite(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        candidates = [
            EmailCandidate(email="jane.doe@acme.com", sources=("scrapegraph_pattern",), domain="acme.com", score=0.9, mx_status=MX_VALID, mx_checked=True),
            EmailCandidate(email="jdoe@acme.com", sources=("scrapegraph_pattern",), domain="acme.com", score=0.5, mx_status=MX_VALID, mx_checked=True),
        ]
        store.save_candidates(lead.lead_id, candidates_to_rows(lead.lead_id, candidates))

        rows = load_candidate_rows(store, lead.lead_id)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].email, "jane.doe@acme.com")  # best-first (rank ascending)
        self.assertEqual(rows[1].email, "jdoe@acme.com")
        self.assertTrue(rows[0].is_best)
        self.assertFalse(rows[1].is_best)

    def test_persisted_candidates_survive_a_reload_of_the_same_sqlite_file(self):
        db_path = Path("/tmp/day6_candidate_persistence_test.db")
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
            rows = load_candidate_rows(store2, lead.lead_id)
            self.assertTrue(rows)
            self.assertTrue(any(r.email == "jane.doe@acme.com" for r in rows))
            store2.close()
        finally:
            db_path.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(db_path) + suffix).unlink(missing_ok=True)

    def test_save_candidates_replaces_previous_set(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        store.save_candidates(lead.lead_id, candidates_to_rows(lead.lead_id, [
            EmailCandidate(email="old@acme.com", domain="acme.com"),
        ]))
        store.save_candidates(lead.lead_id, candidates_to_rows(lead.lead_id, [
            EmailCandidate(email="new@acme.com", domain="acme.com"),
        ]))
        rows = load_candidate_rows(store, lead.lead_id)
        self.assertEqual([r.email for r in rows], ["new@acme.com"])

    def test_process_lead_email_persists_every_candidate_not_just_the_winner(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        process_lead_email(store, lead, mx_checker=FakeMXChecker({"acme.com": MX_VALID}), smtp_checker=FakeSMTPChecker())
        rows = load_candidate_rows(store, lead.lead_id)
        # The primary generator produces multiple pattern-based candidates
        # for one lead+domain; all of them must be persisted, not just the
        # single email written onto Lead.email.
        self.assertGreater(len(rows), 1)
        emails = {r.email for r in rows}
        self.assertIn("jane.doe@acme.com", emails)
        self.assertIn("jdoe@acme.com", emails)

    def test_persisted_row_carries_source_pattern_mx_smtp_score_confidence(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        process_lead_email(
            store, lead,
            mx_checker=FakeMXChecker({"acme.com": MX_VALID}),
            smtp_checker=FakeSMTPChecker({"jane.doe@acme.com": SMTP_EXISTS}),
            enable_smtp=True,
        )
        rows = load_candidate_rows(store, lead.lead_id)
        best = next(r for r in rows if r.email == "jane.doe@acme.com")
        self.assertIn("scrapegraph_pattern", best.sources)
        self.assertTrue(best.patterns)
        self.assertEqual(best.domain, "acme.com")
        self.assertEqual(best.mx_status, MX_VALID)
        self.assertEqual(best.smtp_status, SMTP_EXISTS)
        self.assertGreater(best.score, 0)
        self.assertIn(best.confidence, ("high", "medium", "low", "none"))


# ---------------------------------------------------------------------------
# 2. Candidate ranking (via persisted rows)
# ---------------------------------------------------------------------------


class TestCandidateRankingFromStorage(unittest.TestCase):
    def test_rank_order_preserved_through_persistence(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        candidates = [
            EmailCandidate(email="a@acme.com", score=0.9),
            EmailCandidate(email="b@acme.com", score=0.5),
            EmailCandidate(email="c@acme.com", score=0.1),
        ]
        store.save_candidates(lead.lead_id, candidates_to_rows(lead.lead_id, candidates))
        rows = load_candidate_rows(store, lead.lead_id)
        self.assertEqual([r.email for r in rows], ["a@acme.com", "b@acme.com", "c@acme.com"])
        self.assertEqual([r.rank for r in rows], [0, 1, 2])

    def test_only_the_top_usable_candidate_is_flagged_is_best(self):
        candidates = [
            EmailCandidate(email="dead@acme.com", score=0.95, mx_status=MX_DEAD, mx_checked=True),
            EmailCandidate(email="usable@acme.com", score=0.6, mx_status=MX_VALID, mx_checked=True),
        ]
        # generate_candidates_for_lead ranks by score, so "dead" (higher
        # score) sorts first even though it's disqualified -- is_best must
        # skip over it.
        ranked = sorted(candidates, key=lambda c: c.score, reverse=True)
        rows = candidates_to_rows("lead-1", ranked)
        best_rows = [r for r in rows if r["is_best"]]
        self.assertEqual(len(best_rows), 1)
        self.assertEqual(best_rows[0]["email"], "usable@acme.com")


# ---------------------------------------------------------------------------
# 3. MX valid / dead / unknown
# ---------------------------------------------------------------------------


class TestMXValidationStatus(unittest.TestCase):
    def test_mx_valid_classifies_domain_valid(self):
        c = EmailCandidate(email="a@x.com", mx_status=MX_VALID, mx_checked=True)
        self.assertEqual(classify_validation_status(c), VALIDATION_DOMAIN_VALID)

    def test_mx_dead_classifies_invalid_regardless_of_score(self):
        c = EmailCandidate(email="a@x.com", score=0.99, mx_status=MX_DEAD, mx_checked=True)
        self.assertEqual(classify_validation_status(c), VALIDATION_INVALID)

    def test_mx_unknown_but_checked_classifies_unknown_not_generated(self):
        c = EmailCandidate(email="a@x.com", mx_status=MX_UNKNOWN, mx_checked=True)
        self.assertEqual(classify_validation_status(c), VALIDATION_UNKNOWN)

    def test_never_checked_classifies_generated(self):
        c = EmailCandidate(email="a@x.com", mx_status=MX_UNKNOWN, smtp_status=SMTP_NOT_CHECKED)
        self.assertEqual(classify_validation_status(c), VALIDATION_GENERATED)
        self.assertFalse(c.mx_checked)
        self.assertFalse(c.smtp_checked)

    def test_generate_candidates_for_lead_marks_mx_checked_for_attempted_domains(self):
        lead = make_lead()
        result = generate_candidates_for_lead(
            lead, mx_checker=FakeMXChecker({"acme.com": MX_VALID}), smtp_checker=FakeSMTPChecker()
        )
        self.assertTrue(result.candidates)
        self.assertTrue(all(c.mx_checked for c in result.candidates))


# ---------------------------------------------------------------------------
# 4. SMTP exists / not-exists / unknown
# ---------------------------------------------------------------------------


class TestSMTPValidationStatus(unittest.TestCase):
    def test_smtp_exists_classifies_smtp_confirmed(self):
        c = EmailCandidate(email="a@x.com", mx_status=MX_VALID, mx_checked=True, smtp_status=SMTP_EXISTS, smtp_checked=True)
        self.assertEqual(classify_validation_status(c), VALIDATION_SMTP_CONFIRMED)

    def test_smtp_not_exists_classifies_invalid(self):
        c = EmailCandidate(email="a@x.com", mx_status=MX_VALID, mx_checked=True, smtp_status=SMTP_NOT_EXISTS, smtp_checked=True)
        self.assertEqual(classify_validation_status(c), VALIDATION_INVALID)

    def test_smtp_unknown_after_check_classifies_inconclusive(self):
        c = EmailCandidate(email="a@x.com", mx_status=MX_VALID, mx_checked=True, smtp_status=SMTP_UNKNOWN, smtp_checked=True)
        self.assertEqual(classify_validation_status(c), VALIDATION_SMTP_INCONCLUSIVE)

    def test_smtp_catch_all_classifies_inconclusive_not_confirmed(self):
        c = EmailCandidate(email="a@x.com", mx_status=MX_VALID, mx_checked=True, smtp_status=SMTP_CATCH_ALL, smtp_checked=True)
        self.assertEqual(classify_validation_status(c), VALIDATION_SMTP_INCONCLUSIVE)

    def test_smtp_not_checked_falls_back_to_mx_based_status(self):
        c = EmailCandidate(email="a@x.com", mx_status=MX_VALID, mx_checked=True, smtp_status=SMTP_NOT_CHECKED)
        self.assertEqual(classify_validation_status(c), VALIDATION_DOMAIN_VALID)

    def test_generate_candidates_for_lead_marks_smtp_checked_only_when_enabled(self):
        lead = make_lead()
        result_off = generate_candidates_for_lead(
            lead, mx_checker=FakeMXChecker({"acme.com": MX_VALID}), smtp_checker=FakeSMTPChecker()
        )
        self.assertTrue(all(not c.smtp_checked for c in result_off.candidates))

        result_on = generate_candidates_for_lead(
            lead,
            mx_checker=FakeMXChecker({"acme.com": MX_VALID}),
            smtp_checker=FakeSMTPChecker({"jane.doe@acme.com": SMTP_EXISTS}),
            enable_smtp=True,
        )
        checked = {c.email: c.smtp_checked for c in result_on.candidates}
        self.assertTrue(checked["jane.doe@acme.com"])


# ---------------------------------------------------------------------------
# 5. Best-candidate selection (from storage, no regeneration)
# ---------------------------------------------------------------------------


class TestBestCandidateSelectionFromStorage(unittest.TestCase):
    def test_select_best_row_skips_invalid_and_picks_lowest_rank_usable(self):
        rows = [
            candidate_row_from_dict(candidate_to_row(
                EmailCandidate(email="dead@x.com", mx_status=MX_DEAD, mx_checked=True, score=0.9),
                lead_id="l1", rank=0, is_best=False,
            )),
            candidate_row_from_dict(candidate_to_row(
                EmailCandidate(email="usable@x.com", mx_status=MX_VALID, mx_checked=True, score=0.5),
                lead_id="l1", rank=1, is_best=True,
            )),
        ]
        best = select_best_row(rows)
        self.assertIsNotNone(best)
        self.assertEqual(best.email, "usable@x.com")

    def test_select_best_row_returns_none_when_all_invalid(self):
        rows = [
            candidate_row_from_dict(candidate_to_row(
                EmailCandidate(email="dead@x.com", mx_status=MX_DEAD, mx_checked=True),
                lead_id="l1", rank=0, is_best=False,
            )),
        ]
        self.assertIsNone(select_best_row(rows))

    def test_select_best_row_returns_none_for_empty_list(self):
        self.assertIsNone(select_best_row([]))

    def test_best_candidate_reselectable_without_regenerating(self):
        """Persist candidates once, then select the best purely from
        storage — no generator, MX checker, or SMTP checker involved at
        selection time at all (item 4)."""
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        process_lead_email(store, lead, mx_checker=FakeMXChecker({"acme.com": MX_VALID}), smtp_checker=FakeSMTPChecker())

        # Re-select purely from storage.
        rows = load_candidate_rows(store, lead.lead_id)
        reselected = select_best_row(rows)
        self.assertIsNotNone(reselected)
        self.assertEqual(reselected.email, lead.email)


# ---------------------------------------------------------------------------
# 6. Invalid candidate rejection
# ---------------------------------------------------------------------------


class TestInvalidCandidateRejection(unittest.TestCase):
    def test_dead_domain_candidate_never_becomes_selected_email(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        found = process_lead_email(store, lead, mx_checker=FakeMXChecker({"acme.com": MX_DEAD, "acme.ai": MX_DEAD, "acme.io": MX_DEAD, "acme.co": MX_DEAD}), smtp_checker=FakeSMTPChecker())
        self.assertEqual(found.status, PipelineStatus.EMAIL_NOT_FOUND)
        self.assertEqual(found.email, "")

        rows = load_candidate_rows(store, lead.lead_id)
        self.assertTrue(rows)
        self.assertTrue(all(r.validation_status == VALIDATION_INVALID for r in rows))
        self.assertIsNone(select_best_row(rows))

    def test_smtp_not_exists_candidate_is_excluded_from_best_even_with_valid_mx(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        process_lead_email(
            store, lead,
            mx_checker=FakeMXChecker({"acme.com": MX_VALID}),
            smtp_checker=FakeSMTPChecker({"jane.doe@acme.com": SMTP_NOT_EXISTS}),
            enable_smtp=True,
        )
        rows = load_candidate_rows(store, lead.lead_id)
        top = next(r for r in rows if r.email == "jane.doe@acme.com")
        self.assertEqual(top.validation_status, VALIDATION_INVALID)
        best = select_best_row(rows)
        self.assertIsNotNone(best)
        self.assertNotEqual(best.email, "jane.doe@acme.com")

    def test_a_genuinely_dead_domain_lead_ends_up_email_not_found_end_to_end(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        process_lead_email(store, lead, mx_checker=FakeMXChecker({"acme.com": MX_DEAD, "acme.ai": MX_DEAD, "acme.io": MX_DEAD, "acme.co": MX_DEAD}), smtp_checker=FakeSMTPChecker())
        final = store.get(lead.lead_id)
        self.assertEqual(final.status, PipelineStatus.EMAIL_NOT_FOUND)
        self.assertEqual(final.email, "")
        self.assertNotEqual(final.status, PipelineStatus.EMAIL_VALIDATED)


# ---------------------------------------------------------------------------
# 7. Resume after interruption
# ---------------------------------------------------------------------------


class TestResumeAfterInterruption(unittest.TestCase):
    def test_validation_stage_only_touches_email_candidates_found_leads(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        candidates_lead = make_lead(linkedin_url="https://linkedin.com/in/a")
        store.upsert_lead(candidates_lead)
        process_lead_email(store, candidates_lead, mx_checker=FakeMXChecker({"acme.com": MX_VALID}), smtp_checker=FakeSMTPChecker())
        self.assertEqual(store.get(candidates_lead.lead_id).status, PipelineStatus.EMAIL_CANDIDATES_FOUND)

        still_qualified = make_lead(linkedin_url="https://linkedin.com/in/b")
        store.upsert_lead(still_qualified)

        stats = find_and_validate_pending_leads(store)
        self.assertEqual(stats, {"email_validated": 1, "email_not_found": 0})
        # The still-QUALIFIED lead must be untouched by the validation stage.
        self.assertEqual(store.get(still_qualified.lead_id).status, PipelineStatus.QUALIFIED)

    def test_resuming_validation_after_it_already_ran_is_a_no_op(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        process_lead_email(store, lead, mx_checker=FakeMXChecker({"acme.com": MX_VALID}), smtp_checker=FakeSMTPChecker())
        find_and_validate_pending_leads(store)
        [validated] = store.all()
        self.assertEqual(validated.status, PipelineStatus.EMAIL_VALIDATED)

        # Simulate re-running the stage after a restart.
        stats = find_and_validate_pending_leads(store)
        self.assertEqual(stats, {"email_validated": 0, "email_not_found": 0})
        [still_one] = store.all()
        self.assertEqual(still_one.lead_id, validated.lead_id)
        self.assertEqual(still_one.status, PipelineStatus.EMAIL_VALIDATED)

    def test_interruption_between_generation_and_validation_is_safely_resumable(self):
        """Simulates a process dying right after process_lead_email
        finishes (candidates persisted, lead in EMAIL_CANDIDATES_FOUND) but
        before the validation stage ever runs. A fresh store/process
        picking the DB back up must be able to validate from persisted
        state alone."""
        db_path = Path("/tmp/day6_resume_test.db")
        db_path.unlink(missing_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(db_path) + suffix).unlink(missing_ok=True)
        try:
            store1 = LeadStore(db_path)
            self.addCleanup(store1.close)
            lead = make_lead()
            store1.upsert_lead(lead)
            process_lead_email(store1, lead, mx_checker=FakeMXChecker({"acme.com": MX_VALID}), smtp_checker=FakeSMTPChecker())
            store1.close()  # simulate process death here

            store2 = LeadStore(db_path)
            self.addCleanup(store2.close)
            reloaded = store2.get(lead.lead_id)
            self.assertEqual(reloaded.status, PipelineStatus.EMAIL_CANDIDATES_FOUND)
            validated = validate_and_select_email(store2, reloaded)
            self.assertEqual(validated.status, PipelineStatus.EMAIL_VALIDATED)
            self.assertEqual(validated.email, "jane.doe@acme.com")
            store2.close()
        finally:
            db_path.unlink(missing_ok=True)
            for suffix in ("-wal", "-shm"):
                Path(str(db_path) + suffix).unlink(missing_ok=True)

    def test_full_pipeline_resumes_cleanly_across_both_stages(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)

        from .email_discovery import find_and_score_pending_leads
        stage1 = find_and_score_pending_leads(store, mx_checker=FakeMXChecker({"acme.com": MX_VALID}), smtp_checker=FakeSMTPChecker())
        self.assertEqual(stage1, {"email_candidates_found": 1, "email_not_found": 0})

        stage2 = find_and_validate_pending_leads(store)
        self.assertEqual(stage2, {"email_validated": 1, "email_not_found": 0})

        [final] = store.all()
        self.assertEqual(final.status, PipelineStatus.EMAIL_VALIDATED)


# ---------------------------------------------------------------------------
# 8. State transitions
# ---------------------------------------------------------------------------


class TestStateTransitions(unittest.TestCase):
    def test_email_candidates_found_to_email_validated_is_legal(self):
        self.assertEqual(
            validate_transition(PipelineStatus.EMAIL_CANDIDATES_FOUND, PipelineStatus.EMAIL_VALIDATED),
            PipelineStatus.EMAIL_VALIDATED,
        )

    def test_email_candidates_found_to_email_not_found_is_legal(self):
        self.assertEqual(
            validate_transition(PipelineStatus.EMAIL_CANDIDATES_FOUND, PipelineStatus.EMAIL_NOT_FOUND),
            PipelineStatus.EMAIL_NOT_FOUND,
        )

    def test_qualified_cannot_skip_straight_to_email_validated(self):
        with self.assertRaises(InvalidStateTransition):
            validate_transition(PipelineStatus.QUALIFIED, PipelineStatus.EMAIL_VALIDATED)

    def test_email_validated_is_not_terminal_but_has_no_day6_successor_yet(self):
        # EMAIL_VALIDATED legally continues to EMAIL_GENERATED (Day 7+,
        # campaign generation — explicitly out of scope for Day 6) or
        # VALIDATION_FAILED; it must not be reachable from anywhere else.
        self.assertEqual(
            validate_transition(PipelineStatus.EMAIL_VALIDATED, PipelineStatus.EMAIL_GENERATED),
            PipelineStatus.EMAIL_GENERATED,
        )
        with self.assertRaises(InvalidStateTransition):
            validate_transition(PipelineStatus.EMAIL_NOT_FOUND, PipelineStatus.EMAIL_GENERATED)

    def test_validate_and_select_email_actually_performs_the_transition(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        process_lead_email(store, lead, mx_checker=FakeMXChecker({"acme.com": MX_VALID}), smtp_checker=FakeSMTPChecker())
        candidates_found = store.get(lead.lead_id)
        self.assertEqual(candidates_found.status, PipelineStatus.EMAIL_CANDIDATES_FOUND)

        validated = validate_and_select_email(store, candidates_found)
        self.assertEqual(validated.status, PipelineStatus.EMAIL_VALIDATED)

    def test_no_usable_persisted_candidate_transitions_to_email_not_found_not_validated(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        # Force EMAIL_CANDIDATES_FOUND with only an invalid candidate
        # persisted (simulates candidates that looked fine at generation
        # time but a later audit finds nothing usable).
        store.save_candidates(lead.lead_id, candidates_to_rows(lead.lead_id, [
            EmailCandidate(email="dead@acme.com", domain="acme.com", mx_status=MX_DEAD, mx_checked=True),
        ]))
        store.transition(lead.lead_id, PipelineStatus.EMAIL_CANDIDATES_FOUND)
        stuck_lead = store.get(lead.lead_id)

        result = validate_and_select_email(store, stuck_lead)
        self.assertEqual(result.status, PipelineStatus.EMAIL_NOT_FOUND)
        self.assertEqual(result.email, "")
        self.assertNotEqual(result.status, PipelineStatus.EMAIL_VALIDATED)


# ---------------------------------------------------------------------------
# 9. Day 2-5 regression
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


class TestDay5Regression(unittest.TestCase):
    def test_process_lead_email_still_selects_best_and_reaches_email_candidates_found(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        updated = process_lead_email(store, lead, mx_checker=FakeMXChecker({"acme.com": MX_VALID}), smtp_checker=FakeSMTPChecker())
        self.assertEqual(updated.status, PipelineStatus.EMAIL_CANDIDATES_FOUND)
        self.assertEqual(updated.email, "jane.doe@acme.com")
        self.assertEqual(updated.email_confidence, "high")

    def test_process_lead_email_still_reaches_email_not_found_with_no_candidates(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead(first_name="", last_name="", full_name="", company_name="")
        store.upsert_lead(lead)
        updated = process_lead_email(store, lead)
        self.assertEqual(updated.status, PipelineStatus.EMAIL_NOT_FOUND)
        self.assertEqual(updated.email, "")

    def test_generate_candidates_for_lead_unchanged_shape(self):
        lead = make_lead()
        result = generate_candidates_for_lead(lead, mx_checker=FakeMXChecker(), smtp_checker=FakeSMTPChecker())
        self.assertTrue(result.candidates)
        self.assertIsNotNone(result.best)
        self.assertEqual(result.best.source, "scrapegraph_pattern")


if __name__ == "__main__":
    unittest.main()
