"""Day 6: EMAIL_CANDIDATES_FOUND -> EMAIL_VALIDATED / EMAIL_NOT_FOUND.

    EMAIL_CANDIDATES_FOUND lead
        -> load persisted candidates for this lead_id (LeadStore.list_candidates)
           -- NEVER regenerates or re-scores; Day 5's generation stage
              (email_discovery.process_lead_email) already did that and
              persisted the full ranked list, evidence and all.
        -> select_best_row(): lowest-rank candidate whose validation_status
           isn't INVALID (item 4, item 7)
        -> usable candidate found -> (re-)populate email/email_source/
           email_confidence/email_status from it -> EMAIL_VALIDATED
        -> no usable candidate persisted -> EMAIL_NOT_FOUND (item 9)

This module is the *only* place PipelineStatus.EMAIL_VALIDATED is ever
written. Day 5's process_lead_email (email_discovery.py) transitions
QUALIFIED straight to EMAIL_CANDIDATES_FOUND or EMAIL_NOT_FOUND and never
touches EMAIL_VALIDATED — that split is deliberate (item 8): the candidate-
generation stage and the candidate-validation/ranking stage are two
separate, independently resumable steps, each owning exactly one state
transition.

Nothing in models.py, lead_store.py's Day 4 behavior, or email_discovery.py's
Day 5 behavior is changed by this module — it only adds a new stage that
consumes what Day 5 already persists (see email_discovery.candidates_to_rows
/ candidate_row_from_dict). No Mailfoguess/email-finder-main integration
work happens here; those remain the soft-import seams defined in Day 5,
untouched.
"""

from __future__ import annotations

from .email_discovery import (
    VALIDATION_INVALID,
    CandidateRow,
    candidate_row_from_dict,
    select_best_row,
)
from .lead_store import LeadStore
from .models import Lead, PipelineStatus

# email_status values written by this module — same free-text vocabulary
# email_discovery.py uses (not state-machine-validated; see models.Lead).
EMAIL_STATUS_VALIDATED = "validated"
EMAIL_STATUS_NOT_FOUND = "not_found"


def load_candidate_rows(store: LeadStore, lead_id: str) -> list[CandidateRow]:
    """Read a lead's persisted candidates back as typed CandidateRow
    objects, best-first. Pure read — never touches generation or the
    network/Node checkers."""
    return [candidate_row_from_dict(r) for r in store.list_candidates(lead_id)]


def validate_and_select_email(store: LeadStore, lead: Lead) -> Lead:
    """Run the Day 6 validation stage for one EMAIL_CANDIDATES_FOUND Lead.

    Re-selects the best usable candidate from what's already persisted
    (item 4) and promotes the Lead to EMAIL_VALIDATED, or — if every
    persisted candidate turns out to be INVALID (item 7: a genuinely
    dead/invalid candidate can never be selected) or none were persisted at
    all — transitions to EMAIL_NOT_FOUND instead of falsely marking the
    lead validated (item 9).

    Fields are written via store.save() *before* the state transition,
    exactly like email_discovery.process_lead_email, so a process that dies
    between the two still leaves an EMAIL_CANDIDATES_FOUND lead with its
    (re-)selected email fields already written — safe to resume, since
    calling this again against the same still-EMAIL_CANDIDATES_FOUND lead
    is idempotent (it reads the same persisted rows and picks the same
    winner) and simply finishes the transition (item 10).
    """
    rows = load_candidate_rows(store, lead.lead_id)
    best = select_best_row(rows)

    if best is not None:
        lead.email = best.email
        lead.email_source = "+".join(best.sources) if best.sources else lead.email_source
        lead.email_confidence = best.confidence
        lead.email_status = EMAIL_STATUS_VALIDATED
        store.save(lead)
        return store.transition(lead.lead_id, PipelineStatus.EMAIL_VALIDATED)

    lead.email = ""
    lead.email_confidence = "none"
    lead.email_status = EMAIL_STATUS_NOT_FOUND
    store.save(lead)
    return store.transition(lead.lead_id, PipelineStatus.EMAIL_NOT_FOUND)


def find_and_validate_pending_leads(
    store: LeadStore,
    *,
    campaign_id: str | None = None,
) -> dict[str, int]:
    """Process every Lead currently in EMAIL_CANDIDATES_FOUND for a
    campaign.

    Resumable by construction (item 10), mirroring
    email_discovery.find_and_score_pending_leads: it only ever pulls leads
    still sitting in EMAIL_CANDIDATES_FOUND, so if the process is
    interrupted partway through, the next call picks up exactly the
    remaining EMAIL_CANDIDATES_FOUND leads — leads already moved to
    EMAIL_VALIDATED or EMAIL_NOT_FOUND are never touched or reprocessed, and
    since validation never regenerates candidates, re-running it is cheap
    (no network/Node calls) and deterministic.
    """
    validated = 0
    not_found = 0
    for lead in store.list_by_status(PipelineStatus.EMAIL_CANDIDATES_FOUND, campaign_id=campaign_id):
        updated = validate_and_select_email(store, lead)
        if updated.status == PipelineStatus.EMAIL_VALIDATED:
            validated += 1
        else:
            not_found += 1
    return {"email_validated": validated, "email_not_found": not_found}


def candidate_validation_invalid(row: CandidateRow) -> bool:
    """Small readability helper: is this persisted candidate row disqualified?"""
    return row.validation_status == VALIDATION_INVALID


if __name__ == "__main__":
    import argparse

    from .config import load_env

    ap = argparse.ArgumentParser(
        description="Day 6: validate and select the best persisted email candidate for EMAIL_CANDIDATES_FOUND leads."
    )
    ap.add_argument("--db", default=None, help="Path to the LeadStore SQLite file (default: data/pipeline_state.db)")
    ap.add_argument("--campaign-id", default=None, help="Only process leads for this campaign_id")
    args = ap.parse_args()

    load_env()
    with (LeadStore(args.db) if args.db else LeadStore()) as store:
        stats = find_and_validate_pending_leads(store, campaign_id=args.campaign_id)
        print(f"EMAIL_VALIDATED:  {stats['email_validated']}")
        print(f"EMAIL_NOT_FOUND:  {stats['email_not_found']}")
