"""Shared types for the angel investor collection pipeline."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TypedDict


class InvestorRow(TypedDict, total=False):
    name: str
    location: str
    linkedin_url: str
    profile_title: str
    summary: str
    industries: str
    email: str
    phone: str
    source: str
    company_name: str
    # Company size, stored as free text when a source happens to expose it
    # (e.g. "51-200 employees" or a bare "120"). Almost never populated by
    # the current discovery sources (search snippets rarely state it) — the
    # qualification layer treats an empty value as "no data available" and
    # does not filter on it, rather than rejecting the profile.
    company_size: str
    # Age is NEVER inferred as a fact. `age` is only ever populated as an
    # explicitly labelled *proxy* (e.g. derived from a stated graduation
    # year), and only ever alongside a non-empty `age_source` describing
    # where the proxy came from and an `age_confidence` describing how much
    # to trust it. See quality.AGE_CONFIDENCE_LEVELS.
    age: str
    age_source: str
    age_confidence: str
    # Written by the orchestrator's "qualify" phase (Day 3+ generic
    # qualification layer, quality.qualify_row). "qualified" / "disqualified"
    # / "" (not yet qualified). qualification_reason is always "" when
    # qualified, and a short human-readable explanation when not.
    qualification_status: str
    qualification_reason: str
    # Generic, industry-agnostic evidence audit fields written by the same
    # qualify_row() call — never named after a specific industry/keyword
    # (e.g. never "ai_relevance"), since the keyword/industry dimensions
    # themselves are fully configurable via TargetConfig.
    keyword_relevance: str
    keyword_evidence: str
    industry_evidence: str


FIELDNAMES = [
    "name",
    "location",
    "linkedin_url",
    "profile_title",
    "summary",
    "industries",
    "email",
    "phone",
    "source",
    "company_name",
    "company_size",
    "age",
    "age_source",
    "age_confidence",
    "qualification_status",
    "qualification_reason",
    "keyword_relevance",
    "keyword_evidence",
    "industry_evidence",
]

DISCOVERY_SOURCES = frozenset(
    {
        "ddgs_search",
        "exa_people",
        "webclaw_directory",
        "agentcrawl_directory",
        "crawl4ai_directory",
    }
)

DIRECTORY_SOURCES = frozenset(
    {
        "webclaw_directory",
        "agentcrawl_directory",
        "crawl4ai_directory",
    }
)


# ---------------------------------------------------------------------------
# Day 4: canonical Lead model + pipeline state machine.
#
# InvestorRow (above) stays exactly as-is — it is still what every discovery
# source and the existing scraper/quality code produces and consumes. Lead is
# a *new*, separate, canonical representation that the future
# discovery -> enrichment -> validation -> campaign -> send pipeline is built
# around. `lead_pipeline.py` is the (one-way, for now) bridge that turns an
# InvestorRow into a Lead; nothing here requires discovery sources to change.
# ---------------------------------------------------------------------------


class PipelineStatus(str, Enum):
    """Canonical pipeline states for a Lead.

    Linear happy path:
        DISCOVERED -> QUALIFIED -> EMAIL_CANDIDATES_FOUND -> EMAIL_VALIDATED
        -> EMAIL_GENERATED -> APPROVED -> QUEUED -> SENDING -> SENT

    Day 7 note: EMAIL_GENERATED has *two* outgoing edges into terminal/near-
    terminal states reachable directly from review — APPROVED (accept the
    generated draft) and REJECTED (reject it outright during review,
    without ever having been approved). This is distinct from the
    pre-existing APPROVED -> REJECTED edge, which covers rejecting a draft
    that had already been approved (e.g. a second reviewer overturning an
    earlier approval before it's queued). Both edges are legal and mean
    different things; a lead can reach REJECTED from either state.

    Every other state is a terminal failure/exit state reached from one or
    more points in the happy path (see ALLOWED_TRANSITIONS below). A Lead
    can never skip stages (e.g. DISCOVERED -> SENT is not a legal
    transition). Two terminal states currently have more than one incoming
    edge, each representing a distinct failure mode at a different stage:
      - FILTERED_OUT: from DISCOVERED (fails initial matching) or QUALIFIED
        (fails re-check at qualification time).
      - EMAIL_NOT_FOUND: from QUALIFIED (Day 5: candidate *generation*
        produced nothing usable — no plausible address at all) or from
        EMAIL_CANDIDATES_FOUND (Day 6+: candidates existed but none
        survived validation).
    """

    # Happy path
    DISCOVERED = "DISCOVERED"
    QUALIFIED = "QUALIFIED"
    EMAIL_CANDIDATES_FOUND = "EMAIL_CANDIDATES_FOUND"
    EMAIL_VALIDATED = "EMAIL_VALIDATED"
    EMAIL_GENERATED = "EMAIL_GENERATED"
    APPROVED = "APPROVED"
    QUEUED = "QUEUED"
    SENDING = "SENDING"
    SENT = "SENT"

    # Terminal failure / exit states
    FILTERED_OUT = "FILTERED_OUT"
    EMAIL_NOT_FOUND = "EMAIL_NOT_FOUND"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    GENERATION_FAILED = "GENERATION_FAILED"
    REJECTED = "REJECTED"
    SEND_FAILED = "SEND_FAILED"
    CANCELLED = "CANCELLED"


# Explicit adjacency list: current state -> set of legal next states. This is
# the entire state machine. Anything not listed here as a legal edge is an
# illegal transition (including skipping stages, moving backwards, or moving
# out of a terminal state).
ALLOWED_TRANSITIONS: dict[PipelineStatus, frozenset[PipelineStatus]] = {
    PipelineStatus.DISCOVERED: frozenset(
        {PipelineStatus.QUALIFIED, PipelineStatus.FILTERED_OUT}
    ),
    PipelineStatus.QUALIFIED: frozenset(
        {
            PipelineStatus.EMAIL_CANDIDATES_FOUND,
            # Added in Day 5: a QUALIFIED lead for whom candidate
            # generation finds nothing usable (no name/company to work
            # from, or every generated candidate is disqualified by MX/SMTP
            # evidence) exits directly to EMAIL_NOT_FOUND rather than
            # passing through EMAIL_CANDIDATES_FOUND with nothing to show
            # for it. See email_discovery.process_lead_email.
            PipelineStatus.EMAIL_NOT_FOUND,
            PipelineStatus.FILTERED_OUT,
        }
    ),
    PipelineStatus.EMAIL_CANDIDATES_FOUND: frozenset(
        {PipelineStatus.EMAIL_VALIDATED, PipelineStatus.EMAIL_NOT_FOUND}
    ),
    PipelineStatus.EMAIL_VALIDATED: frozenset(
        {
            PipelineStatus.EMAIL_GENERATED,
            PipelineStatus.VALIDATION_FAILED,
            # Day 7: mirrors every other stage's failure-branch pattern
            # (QUALIFIED -> {..., EMAIL_NOT_FOUND},
            # EMAIL_CANDIDATES_FOUND -> {..., EMAIL_NOT_FOUND}): the
            # attempted transition is EMAIL_VALIDATED -> EMAIL_GENERATED,
            # so a rendering failure branches directly off EMAIL_VALIDATED,
            # not off the state that would only exist had it succeeded.
            PipelineStatus.GENERATION_FAILED,
        }
    ),
    PipelineStatus.EMAIL_GENERATED: frozenset(
        {
            PipelineStatus.APPROVED,
            # Day 7: a reviewer can reject a freshly generated draft
            # directly, without it ever passing through APPROVED first.
            PipelineStatus.REJECTED,
        }
    ),
    PipelineStatus.APPROVED: frozenset(
        {PipelineStatus.QUEUED, PipelineStatus.REJECTED}
    ),
    PipelineStatus.QUEUED: frozenset(
        {PipelineStatus.SENDING, PipelineStatus.CANCELLED}
    ),
    PipelineStatus.SENDING: frozenset(
        {PipelineStatus.SENT, PipelineStatus.SEND_FAILED}
    ),
    # Terminal states: no outgoing transitions.
    PipelineStatus.SENT: frozenset(),
    PipelineStatus.FILTERED_OUT: frozenset(),
    PipelineStatus.EMAIL_NOT_FOUND: frozenset(),
    PipelineStatus.VALIDATION_FAILED: frozenset(),
    PipelineStatus.GENERATION_FAILED: frozenset(),
    PipelineStatus.REJECTED: frozenset(),
    PipelineStatus.SEND_FAILED: frozenset(),
    PipelineStatus.CANCELLED: frozenset(),
}

TERMINAL_STATUSES: frozenset[PipelineStatus] = frozenset(
    status for status, nexts in ALLOWED_TRANSITIONS.items() if not nexts
)

# Non-terminal ("in flight") statuses, in happy-path order. Useful for
# resuming: "what's the earliest stage with unfinished work?"
ACTIVE_STATUSES: tuple[PipelineStatus, ...] = (
    PipelineStatus.DISCOVERED,
    PipelineStatus.QUALIFIED,
    PipelineStatus.EMAIL_CANDIDATES_FOUND,
    PipelineStatus.EMAIL_VALIDATED,
    PipelineStatus.EMAIL_GENERATED,
    PipelineStatus.APPROVED,
    PipelineStatus.QUEUED,
    PipelineStatus.SENDING,
)


class InvalidStateTransition(Exception):
    """Raised when a Lead's pipeline_status is asked to move to an illegal state."""

    def __init__(self, current: "PipelineStatus | str", target: "PipelineStatus | str"):
        self.current = current
        self.target = target
        cur_label = current.value if isinstance(current, PipelineStatus) else current
        tgt_label = target.value if isinstance(target, PipelineStatus) else target
        super().__init__(
            f"Illegal pipeline transition: {cur_label} -> {tgt_label} is not allowed"
        )


def coerce_status(value: "PipelineStatus | str") -> PipelineStatus:
    """Accept either a PipelineStatus or its string value; raise ValueError if unknown."""
    if isinstance(value, PipelineStatus):
        return value
    try:
        return PipelineStatus(str(value))
    except ValueError as exc:
        raise ValueError(f"Unknown pipeline status: {value!r}") from exc


def validate_transition(
    current: "PipelineStatus | str", target: "PipelineStatus | str"
) -> PipelineStatus:
    """Return `target` as a PipelineStatus if current -> target is legal.

    Raises InvalidStateTransition otherwise (including no-op self-transitions,
    which are not legal — every transition must move the state machine
    forward to a genuinely different state).
    """
    cur = coerce_status(current)
    tgt = coerce_status(target)
    if tgt not in ALLOWED_TRANSITIONS.get(cur, frozenset()):
        raise InvalidStateTransition(cur, tgt)
    return tgt


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Canonical field order for the Lead model — matches the Day 4 spec exactly,
# plus a handful of preserved/legacy fields appended at the end (phone,
# discovery_source) and bookkeeping fields (lead_id, identity_key) needed to
# make the model persistable and dedupe-able.
LEAD_FIELDNAMES = [
    "lead_id",
    "first_name",
    "last_name",
    "full_name",
    "job_title",
    "company_name",
    "company_domain",
    "company_size",
    "industry",
    "location",
    "linkedin_url",
    "profile_summary",
    "age",
    "age_source",
    "age_confidence",
    "email",
    "email_status",
    "email_source",
    "email_confidence",
    "campaign_id",
    "pipeline_status",
    "created_at",
    "updated_at",
    # Preserved from InvestorRow because they're still useful downstream,
    # even though they weren't in the Day 4 field spec verbatim.
    "phone",
    "discovery_source",
    # Dedup identity (see lead_pipeline.compute_identity_key). Not part of
    # the user-facing spec fields, but required to make deduplication and
    # SQLite upserts work; stored alongside the record rather than
    # recomputed on every read.
    "identity_key",
    # Passthrough evidence fields (see Lead dataclass docstring above).
    "keyword_evidence",
    "industry_evidence",
    # Evidence-based BUND / champion / relevance scoring (Aug 2026 sales-call
    # requirements) -- a JSON blob produced by qualification_scoring.py.
    # Additive: "" means "not yet scored" (e.g. a Lead created before this
    # field existed, or scored before enrichment ran), never "no signal
    # found" (that's represented *inside* the JSON as SIGNAL_UNKNOWN once
    # scoring has actually run). See qualification_scoring.build_qualification_evidence.
    "qualification_evidence",
]


@dataclass
class Lead:
    """The one canonical Lead record used from discovery through sending.

    Field values are always plain strings (never None) so this round-trips
    cleanly through SQLite/CSV without special-casing NULLs; "unknown"/"not
    yet known" is represented as "", exactly like InvestorRow already does.
    """

    lead_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    first_name: str = ""
    last_name: str = ""
    full_name: str = ""

    job_title: str = ""
    company_name: str = ""
    company_domain: str = ""
    company_size: str = ""
    industry: str = ""
    location: str = ""

    linkedin_url: str = ""
    profile_summary: str = ""

    # Age is a labelled proxy, never an invented fact — same contract as
    # InvestorRow.age / age_source / age_confidence (see quality.py).
    age: str = ""
    age_source: str = ""
    age_confidence: str = ""

    # Email enrichment fields (populated starting Day 5+; left blank here).
    # email_status is a free-text progress label distinct from
    # pipeline_status (e.g. "unknown", "candidates_found", "valid",
    # "invalid", "not_found") — it is NOT state-machine-validated, only
    # pipeline_status is.
    email: str = ""
    email_status: str = "unknown"
    email_source: str = ""
    email_confidence: str = ""

    campaign_id: str = ""
    pipeline_status: str = PipelineStatus.DISCOVERED.value

    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    # Preserved legacy fields.
    phone: str = ""
    discovery_source: str = ""

    # Dedup identity key — see lead_pipeline.compute_identity_key(). Empty
    # string means "no reliable identity available" (dedup skipped for this
    # record).
    identity_key: str = ""

    # Carried over from InvestorRow's qualify_row() side-effect fields (see
    # models.py InvestorRow docstring) so qualification_scoring.py can
    # (re)compute BUND/champion evidence from a Lead alone, at any later
    # point, without needing the original CSV row in scope. Pure passthrough
    # -- never used by matches_target_criteria()/qualify_row() themselves,
    # so this has zero effect on pass/fail qualification.
    keyword_evidence: str = ""
    industry_evidence: str = ""

    # Evidence-based BUND/champion/relevance scoring, JSON-encoded (see
    # qualification_scoring.py). "" means not yet scored.
    qualification_evidence: str = ""

    def __post_init__(self) -> None:
        # Normalize pipeline_status to the enum's canonical string value so
        # a Lead built with a PipelineStatus enum and one built with a plain
        # string always compare/serialize identically.
        self.pipeline_status = coerce_status(self.pipeline_status).value
        if not self.full_name:
            self.full_name = " ".join(p for p in (self.first_name, self.last_name) if p).strip()

    @property
    def status(self) -> PipelineStatus:
        return PipelineStatus(self.pipeline_status)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Lead":
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in known and v is not None}
        return cls(**clean)
