"""Day 4: discovery -> Lead normalization, dedup, and qualification bridge.

This module is the seam between the *existing* discovery/scraper code (which
still speaks InvestorRow, unchanged) and the *new* canonical Lead model +
pipeline state machine (models.py) + persistence (lead_store.py).

Discovery sources do not need to know anything about Lead, pipeline states,
email-finding, or Gmail. They keep producing InvestorRow dicts exactly as
before. This module is the only place that converts InvestorRow -> Lead:

    Discovery source -> InvestorRow -> normalize_investor_row() -> Lead
        -> LeadStore.upsert_lead() -> DISCOVERED
        -> qualify_lead() -> QUALIFIED / FILTERED_OUT

Nothing downstream of QUALIFIED is implemented yet (email-finder, Mailfoguess,
validation, Gmail, campaigns) — that's Day 5+.
"""

from __future__ import annotations

import re
from typing import Iterable

from .lead_store import LeadStore
from .models import InvestorRow, Lead, PipelineStatus
from .qualification_scoring import qualification_evidence_json
from .quality import linkedin_slug, matches_target_criteria, normalize_linkedin
from .target_config import TargetConfig
from .config import get_active_target

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


# ---------------------------------------------------------------------------
# Name splitting
# ---------------------------------------------------------------------------


def split_name(name: str) -> tuple[str, str]:
    """Split a free-text full name into (first_name, last_name).

    Best-effort only — InvestorRow only ever carries a single "name" field.
    A single-token name (e.g. a mononym, or a bad parse) becomes
    (name, "") rather than being dropped.
    """
    cleaned = re.sub(r"\s+", " ", (name or "").strip())
    if not cleaned:
        return "", ""
    parts = cleaned.split(" ")
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


# ---------------------------------------------------------------------------
# Deduplication identity
# ---------------------------------------------------------------------------


def _normalize_key_text(value: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace — for identity keys only."""
    text = (value or "").strip().lower()
    text = _NON_ALNUM_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def compute_identity_key(*, linkedin_url: str = "", full_name: str = "", company_name: str = "") -> str:
    """Compute the dedup identity for a Lead.

    Preference order (per Day 4 spec):
      1. normalized LinkedIn URL (as its canonical slug) — primary identity.
      2. normalized "full name + company" — fallback when no LinkedIn URL.
      3. "" (no reliable identity; caller should not dedupe this record).

    This is deliberately the *only* place identity is computed, so discovery
    sources, resumed runs, and re-ingestion all agree on what makes two Lead
    records "the same person" — the same guarantee normalize_linkedin() /
    linkedin_slug() already give InvestorRow-based dedup in quality.py.
    """
    li = normalize_linkedin(linkedin_url or "")
    slug = linkedin_slug(li) if li else ""
    if slug:
        return f"li:{slug}"

    name_key = _normalize_key_text(full_name)
    company_key = _normalize_key_text(company_name)
    if name_key and company_key:
        return f"nc:{name_key}|{company_key}"

    return ""


# ---------------------------------------------------------------------------
# InvestorRow -> Lead normalization
# ---------------------------------------------------------------------------


def normalize_investor_row(row: InvestorRow, *, campaign_id: str = "") -> Lead:
    """Convert one InvestorRow (discovery-source output) into a canonical Lead.

    Always produces a Lead in DISCOVERED status — qualification is a separate
    explicit step (qualify_lead / qualify_pending_leads below), matching the
    Day 4 flow: Discovery source -> Normalize -> Lead -> Qualification ->
    QUALIFIED.
    """
    first_name, last_name = split_name(row.get("name", "") or "")
    li = normalize_linkedin(row.get("linkedin_url", "") or "")

    identity_key = compute_identity_key(
        linkedin_url=li,
        full_name=row.get("name", "") or "",
        company_name=row.get("company_name", "") or "",
    )

    return Lead(
        first_name=first_name,
        last_name=last_name,
        job_title=row.get("profile_title", "") or "",
        company_name=row.get("company_name", "") or "",
        company_domain="",  # populated by email-finder integration (Day 5+)
        company_size=row.get("company_size", "") or "",
        industry=row.get("industries", "") or "",
        location=row.get("location", "") or "",
        linkedin_url=li,
        profile_summary=row.get("summary", "") or "",
        age=row.get("age", "") or "",
        age_source=row.get("age_source", "") or "",
        age_confidence=row.get("age_confidence", "") or "",
        email=row.get("email", "") or "",
        campaign_id=campaign_id,
        pipeline_status=PipelineStatus.DISCOVERED.value,
        phone=row.get("phone", "") or "",
        discovery_source=row.get("source", "") or "",
        identity_key=identity_key,
        keyword_evidence=row.get("keyword_evidence", "") or "",
        industry_evidence=row.get("industry_evidence", "") or "",
    )


# ---------------------------------------------------------------------------
# Ingestion (resumable): InvestorRow batch -> stored Lead records
# ---------------------------------------------------------------------------


def ingest_discovery_rows(
    store: LeadStore,
    rows: Iterable[InvestorRow],
    *,
    campaign_id: str,
) -> dict[str, int]:
    """Normalize + upsert a batch of discovery rows as Leads.

    Safe to call repeatedly (e.g. once per discovery source, or re-run after
    an interruption): re-ingesting a row that already produced a Lead just
    updates that Lead's blank fields and never regresses its pipeline_status
    (see LeadStore.upsert_lead), so a lead that's already progressed past
    DISCOVERED is left alone rather than being reset.
    """
    created = 0
    updated = 0
    no_identity = 0
    for row in rows:
        lead = normalize_investor_row(row, campaign_id=campaign_id)
        if not lead.identity_key:
            no_identity += 1
        _, was_created = store.upsert_lead(lead)
        if was_created:
            created += 1
        else:
            updated += 1
    return {"created": created, "updated": updated, "no_identity": no_identity}


# ---------------------------------------------------------------------------
# Qualification: DISCOVERED -> QUALIFIED / FILTERED_OUT
# ---------------------------------------------------------------------------


def _as_investor_row_for_matching(lead: Lead) -> InvestorRow:
    """Adapt a Lead's fields back into the shape matches_target_criteria()
    (an InvestorRow-shaped function) expects, without changing that function
    or duplicating its matching logic here.
    """
    return {  # type: ignore[return-value]
        "profile_title": lead.job_title,
        "summary": lead.profile_summary,
        "industries": lead.industry,
        "company_name": lead.company_name,
        "location": lead.location,
        "company_size": lead.company_size,
        "age": lead.age,
        "age_confidence": lead.age_confidence,
        # Passthrough only -- matches_target_criteria() does not read these,
        # but qualification_scoring.build_qualification_evidence() does.
        "keyword_evidence": lead.keyword_evidence,
        "industry_evidence": lead.industry_evidence,
    }


def qualify_lead(store: LeadStore, lead: Lead, target: TargetConfig | None = None) -> Lead:
    """Move a single DISCOVERED Lead to QUALIFIED or FILTERED_OUT.

    Reuses the existing, unchanged Day 3 matches_target_criteria() so
    qualification logic lives in exactly one place -- pass/fail is never
    touched or weakened by the scoring step below.

    Additive step (Aug 2026): also computes and persists an evidence-based
    BUND/champion/relevance scoring record (qualification_scoring.py)
    alongside the transition, purely for transparency/prioritization. This
    NEVER changes whether a lead is QUALIFIED or FILTERED_OUT.
    """
    row = _as_investor_row_for_matching(lead)
    effective_target = target or get_active_target()
    lead.qualification_evidence = qualification_evidence_json(row, effective_target)
    store.save(lead)
    if matches_target_criteria(row, target):
        return store.transition(lead.lead_id, PipelineStatus.QUALIFIED)
    return store.transition(lead.lead_id, PipelineStatus.FILTERED_OUT)


def qualify_pending_leads(
    store: LeadStore,
    *,
    campaign_id: str | None = None,
    target: TargetConfig | None = None,
) -> dict[str, int]:
    """Qualify every Lead currently sitting in DISCOVERED for a campaign.

    Resumable by construction: it only ever pulls leads still in DISCOVERED,
    so if the process is interrupted partway through, the next call simply
    picks up the remaining DISCOVERED leads — nothing already QUALIFIED or
    FILTERED_OUT is touched or reprocessed.
    """
    qualified = 0
    filtered_out = 0
    for lead in store.list_by_status(PipelineStatus.DISCOVERED, campaign_id=campaign_id):
        result = qualify_lead(store, lead, target)
        if result.status == PipelineStatus.QUALIFIED:
            qualified += 1
        else:
            filtered_out += 1
    return {"qualified": qualified, "filtered_out": filtered_out}
