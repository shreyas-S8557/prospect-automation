"""Day 7: canonical Campaign model + template validation/rendering.

    Campaign (subject_template, body_template)
        -> validated at creation time: every {{variable}} used must be one
           of the six supported personalization variables
        -> render_template() substitutes variables deterministically from a
           Lead's fields at generation time (see email_generation.py)

This module owns the *template* half of Day 7 (items 1-4 of the spec): the
Campaign model itself, campaign creation/configuration, and the small
{{variable}} substitution engine shared by preview and generation. The
*lead-personalization / state-machine / draft-review* half (items 5-10) is
in email_generation.py, which imports render_template + SUPPORTED_VARIABLES
from here rather than duplicating them.

Nothing in models.py's Lead/PipelineStatus, lead_pipeline.py, email_discovery.py,
or email_validation.py is touched by this module. lead_store.py gained a
small, generic (dict-in/dict-out) campaigns table — see the "Day 7" section
there — so this module has a persistence home without introducing an import
cycle (lead_store.py never imports campaign.py).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from .lead_store import LeadStore
from .models import utc_now_iso

# ---------------------------------------------------------------------------
# Supported template variables (Day 7 spec item 4) — deliberately a closed
# set. Anything else in a template is a configuration error caught at
# campaign-creation time, not a silent no-op or a runtime crash during
# generation.
# ---------------------------------------------------------------------------

SUPPORTED_VARIABLES: frozenset[str] = frozenset(
    {
        "first_name",
        "last_name",
        "company_name",
        "job_title",
        "location",
        "industry",
        # Derived, evidence-grounded fields computed by
        # email_generation.grounded_context() -- never raw Lead passthrough.
        # A campaign template may use these instead of (or alongside) the
        # six raw fields above to get personalization that's automatically
        # evidence-aware: opening_line/value_line are composed only from
        # verified Lead fields (job_title/company_name/industry/location),
        # and never reference a company_name that couldn't be confidently
        # established (see email_generation.is_confident_company). See
        # DEFAULT_SUBJECT_TEMPLATE / DEFAULT_BODY_TEMPLATE below.
        "opening_line",
        "value_line",
        "subject_hook",
        # Aug 2026: sharper, 3-part first-touch structure (hook -> evidence/
        # hypothesis -> low-friction CTA) computed by
        # email_generation.compose_outreach_personalization(). Purely
        # additive alongside opening_line/value_line/subject_hook above --
        # existing campaigns/templates using only those three are
        # completely unaffected.
        "hook_line",
        "evidence_line",
        "cta_line",
        "problem_subject",
    }
)

# Matches {{var}}, {{ var }}, {{  var  }} — a bare word only (no dotted
# paths, no filters); deliberately minimal so it can't be abused as a
# general-purpose template language.
_VAR_PATTERN = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

CAMPAIGN_FIELDNAMES = [
    "campaign_id",
    "name",
    "description",
    "subject_template",
    "body_template",
    "sender_name",
    "status",
    "created_at",
    "updated_at",
    "case_studies_json",
    "pain_points_json",
]

CAMPAIGN_STATUS_ACTIVE = "active"
CAMPAIGN_STATUS_ARCHIVED = "archived"

# ---------------------------------------------------------------------------
# Deliberately generic fallback templates, used only when nothing more
# specific is supplied (see ensure_campaign below). These exist so the
# pipeline can always produce a *valid* Campaign without any vertical- or
# customer-specific copy baked into generic code -- callers (e.g. a
# TargetConfig with its own email_subject_template/email_body_template) are
# always free to override them. Every {{variable}} used here is one of
# SUPPORTED_VARIABLES, so they pass validate_template() unchanged.
#
# Built entirely from {{opening_line}}/{{value_line}}/{{subject_hook}} --
# derived, evidence-grounded sentences computed per-lead by
# email_generation.grounded_context() -- rather than splicing raw
# {{company_name}} into static copy. That per-lead composition is what
# keeps an uncertain/invalid company_name (a university, a bare industry
# term like "AI", ...) out of the email: grounded_context() only ever
# builds a company-referencing opening_line when
# email_generation.is_confident_company() says the evidence supports it,
# and falls back to role/industry/generic phrasing otherwise -- see
# email_generation.py for that logic.
# ---------------------------------------------------------------------------

DEFAULT_SUBJECT_TEMPLATE = "{{subject_hook}}"
DEFAULT_BODY_TEMPLATE = (
    "Hi {{first_name}},\n\n"
    "{{opening_line}}\n\n"
    "{{value_line}}\n\n"
    "Best"
)

# ---------------------------------------------------------------------------
# Aug 2026: sharper first-touch defaults (see email_generation.py's
# compose_outreach_personalization) -- a problem/outcome-oriented subject,
# a role/industry/company-aware hook, an evidence-or-hedged-hypothesis
# line, and a low-friction (non-"quick call"-by-default) CTA. This is what
# new campaigns are created with going forward (see
# app/services/campaign_service.py); DEFAULT_SUBJECT_TEMPLATE/
# DEFAULT_BODY_TEMPLATE above are kept unchanged for backward compatibility
# with campaigns/tests already built against them.
# ---------------------------------------------------------------------------

SHARP_SUBJECT_TEMPLATE = "{{problem_subject}}"
SHARP_BODY_TEMPLATE = (
    "Hi {{first_name}},\n\n"
    "{{hook_line}}\n\n"
    "{{evidence_line}}\n\n"
    "{{cta_line}}\n\n"
    "Best"
)


class UnsupportedTemplateVariable(ValueError):
    """Raised when a template references a {{variable}} outside SUPPORTED_VARIABLES."""

    def __init__(self, variable: str):
        self.variable = variable
        super().__init__(
            f"Unsupported template variable '{{{{{variable}}}}}' — supported "
            f"variables are: {', '.join(sorted(SUPPORTED_VARIABLES))}"
        )


def extract_template_variables(template: str) -> set[str]:
    """All {{variable}} tokens referenced in a template, deduplicated."""
    return set(_VAR_PATTERN.findall(template or ""))


def validate_template(template: str) -> None:
    """Raise UnsupportedTemplateVariable if `template` references anything
    outside SUPPORTED_VARIABLES. Pure validation — never mutates or renders."""
    unknown = extract_template_variables(template) - SUPPORTED_VARIABLES
    if unknown:
        raise UnsupportedTemplateVariable(sorted(unknown)[0])


def render_template(template: str, context: dict[str, str]) -> str:
    """Deterministically substitute every {{variable}} in `template` from
    `context`.

    A variable present in the template but absent from `context` renders as
    "" (this is how "missing optional fields" — e.g. a Lead with no
    job_title — are handled: Lead fields are always plain strings, never
    None, so an empty field naturally substitutes to ""). This function
    performs no validation of its own; call validate_template() first (as
    create_campaign does) to catch unsupported variables early.
    """

    def _substitute(match: re.Match) -> str:
        return context.get(match.group(1), "")

    return _VAR_PATTERN.sub(_substitute, template or "")


@dataclass
class Campaign:
    """The canonical Campaign record: a named, reusable subject/body
    template pair that Leads are personalized against.

    Like Lead, field values are always plain strings so this round-trips
    cleanly through SQLite without special-casing NULLs.
    """

    campaign_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = ""
    description: str = ""
    subject_template: str = ""
    body_template: str = ""
    sender_name: str = ""
    status: str = CAMPAIGN_STATUS_ACTIVE
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    # Aug 2026: optional, admin-supplied VERIFIED case studies/results this
    # campaign may draw on for outreach copy. JSON-encoded list of
    # {"industries": [...], "keywords": [...], "text": "..."}. "[]" (the
    # default) means none configured -- email_generation.py's case-study
    # selection must never fabricate one in that case, only fall back to a
    # clearly-hedged hypothesis sentence.
    case_studies_json: str = "[]"
    # Aug 2026: optional, admin-supplied, industry-tailored problem
    # hypotheses ("make it tailored to any industry"). JSON-encoded list of
    # {"industries": [...], "keywords": [...], "roles": [...], "label":
    # "...", "phrase": "..."}. "[]" (the default) means the generator falls
    # back to its industry-neutral generic pool -- never a hardcoded
    # tech/SaaS-specific one. See email_generation.select_pain_angle().
    pain_points_json: str = "[]"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Campaign":
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in known and v is not None}
        return cls(**clean)

    @property
    def case_studies(self) -> list[dict[str, Any]]:
        """Parsed case_studies_json, defensively -- malformed/empty JSON
        yields an empty list rather than raising, since "no case studies
        configured" must always be a safe, valid state."""
        import json

        try:
            parsed = json.loads(self.case_studies_json or "[]")
            return parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            return []

    @case_studies.setter
    def case_studies(self, value: list[dict[str, Any]]) -> None:
        import json

        self.case_studies_json = json.dumps(value or [])

    @property
    def pain_points(self) -> list[dict[str, Any]]:
        """Parsed pain_points_json, defensively -- same "never raise, just
        treat malformed/empty as none configured" contract as case_studies
        above."""
        import json

        try:
            parsed = json.loads(self.pain_points_json or "[]")
            return parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            return []

    @pain_points.setter
    def pain_points(self, value: list[dict[str, Any]]) -> None:
        import json

        self.pain_points_json = json.dumps(value or [])

    @property
    def variables_used(self) -> set[str]:
        """Union of {{variable}} tokens referenced across both templates."""
        return extract_template_variables(self.subject_template) | extract_template_variables(
            self.body_template
        )


def create_campaign(
    name: str,
    subject_template: str,
    body_template: str,
    *,
    description: str = "",
    sender_name: str = "",
    status: str = CAMPAIGN_STATUS_ACTIVE,
    campaign_id: str | None = None,
    case_studies: list[dict] | None = None,
    pain_points: list[dict] | None = None,
) -> Campaign:
    """Create (but do not persist) a validated Campaign.

    Raises ValueError if name/subject_template/body_template is blank, and
    UnsupportedTemplateVariable if either template references a variable
    outside SUPPORTED_VARIABLES. This is the only place templates are
    expected to be validated — email_generation.render_email trusts a
    Campaign it's handed was created this way (or was loaded back from
    storage, where it was already validated once).
    """
    if not name.strip():
        raise ValueError("Campaign name is required")
    if not subject_template.strip():
        raise ValueError("subject_template is required")
    if not body_template.strip():
        raise ValueError("body_template is required")

    validate_template(subject_template)
    validate_template(body_template)

    kwargs: dict[str, Any] = dict(
        name=name,
        description=description,
        subject_template=subject_template,
        body_template=body_template,
        sender_name=sender_name,
        status=status,
    )
    if campaign_id:
        kwargs["campaign_id"] = campaign_id
    campaign = Campaign(**kwargs)
    if case_studies:
        campaign.case_studies = case_studies
    if pain_points:
        campaign.pain_points = pain_points
    return campaign


# -- persistence helpers (thin wrappers over LeadStore's generic dict rows) --


def save_campaign(store: LeadStore, campaign: Campaign) -> Campaign:
    store.save_campaign(campaign.to_dict())
    return campaign


def load_campaign(store: LeadStore, campaign_id: str) -> Campaign | None:
    row = store.get_campaign(campaign_id)
    return Campaign.from_dict(row) if row else None


def list_campaigns(store: LeadStore) -> list[Campaign]:
    return [Campaign.from_dict(row) for row in store.list_campaigns()]


def ensure_campaign(
    store: LeadStore,
    campaign_id: str,
    *,
    name: str | None = None,
    description: str = "",
    subject_template: str | None = None,
    body_template: str | None = None,
    sender_name: str = "",
) -> Campaign:
    """Idempotently make sure a Campaign row exists for `campaign_id`.

    This is the missing half of the Campaign lifecycle: campaign.py already
    had create/save/load, but nothing *upstream* (e.g. ingestion) ever
    called them, so leads could exist in a campaign with no matching
    Campaign row for email_generation/email_sending to load.

    Behaviour:
      - If a Campaign already exists for `campaign_id`, it is returned
        completely unchanged -- ensure_campaign NEVER overwrites an
        existing campaign (e.g. one hand-authored via create_campaign.py
        or the UI), destructively or otherwise. This also makes it safe to
        call on every ingestion run: the first run creates the campaign,
        every subsequent run with the same campaign_id is a no-op that
        reuses the same row instead of creating a duplicate.
      - If none exists, a new Campaign is created and persisted using the
        supplied name/templates, falling back to DEFAULT_SUBJECT_TEMPLATE /
        DEFAULT_BODY_TEMPLATE when template text isn't supplied. Those
        defaults are intentionally generic (no vertical-specific copy) --
        real campaign copy should come from the caller (e.g. TargetConfig's
        optional email_subject_template/email_body_template fields, or
        create_campaign.py run afterwards to replace the placeholder copy).

    Raises UnsupportedTemplateVariable if a supplied template references a
    variable outside SUPPORTED_VARIABLES -- same validation create_campaign
    always applies, just surfaced here too since callers may be passing
    templates through from an external config file.
    """
    existing = load_campaign(store, campaign_id)
    if existing is not None:
        return existing

    campaign = create_campaign(
        (name or "").strip() or campaign_id,
        subject_template or DEFAULT_SUBJECT_TEMPLATE,
        body_template or DEFAULT_BODY_TEMPLATE,
        description=description,
        sender_name=sender_name,
        campaign_id=campaign_id,
    )
    return save_campaign(store, campaign)
