"""Day 7: EMAIL_VALIDATED -> EMAIL_GENERATED -> APPROVED / REJECTED.

    EMAIL_VALIDATED lead + Campaign
        -> render_email(): deterministic {{variable}} substitution from the
           Lead's own fields (item 5) -- never touches the network or an LLM
        -> persist the exact rendered subject/body as an EmailJob, keyed
           1:1 on lead_id (item 7)
        -> ONLY THEN transition the Lead to EMAIL_GENERATED (item 6) -- a
           lead is never moved to EMAIL_GENERATED with nothing persisted
           for it, and rendering failures divert to GENERATION_FAILED
           instead
        -> Review: preview / edit the persisted draft (items 8-9)
        -> approve_email() -> APPROVED, or reject_email() -> REJECTED,
           singly or in bulk (items 9-10)

This module is the *only* place PipelineStatus.EMAIL_GENERATED is ever
written, mirroring the ownership convention set by email_discovery.py
(EMAIL_CANDIDATES_FOUND) and email_validation.py (EMAIL_VALIDATED): each
pipeline stage owns exactly one forward transition. Nothing in models.py's
Lead/PipelineStatus (beyond the additive EMAIL_GENERATED -> REJECTED edge
described in models.py itself), lead_pipeline.py, email_discovery.py, or
email_validation.py is changed here. Gmail sending is explicitly out of
scope (Day 8) -- there is no code path anywhere in this module that sends
anything.
"""

from __future__ import annotations

import logging
import re
import uuid
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .campaign import Campaign, render_template
from .lead_store import LeadStore
from .models import InvalidStateTransition, Lead, PipelineStatus, utc_now_iso, validate_transition
from .qualification_scoring import seniority_band
from .quality import (
    is_non_company_org,
    is_valid_company_name,
    is_valid_person_name,
)

log = logging.getLogger("pipeline.email_generation")

# review_status values stored on EmailJob rows. Free-text, mirroring the
# email_status convention on Lead (not state-machine-validated itself --
# PipelineStatus is the state machine; review_status is just a convenient,
# queryable mirror of "which side of review is this draft on" that lets
# list_email_jobs() filter without joining against leads).
REVIEW_PENDING = "PENDING"
REVIEW_APPROVED = "APPROVED"
REVIEW_REJECTED = "REJECTED"

EMAIL_JOB_FIELDNAMES = [
    "job_id",
    "lead_id",
    "campaign_id",
    "subject",
    "body",
    "review_status",
    "edited",
    "rejection_reason",
    "generated_at",
    "reviewed_at",
    "created_at",
    "updated_at",
    "metadata_json",
]


class EmailGenerationError(Exception):
    """Raised (and turned into a GENERATION_FAILED transition) when
    rendering a campaign's templates against a lead doesn't produce usable
    content -- e.g. an empty subject or body after substitution."""


class NoGeneratedEmail(ValueError):
    """Raised by review operations (preview persistence, edit, approve,
    reject) when there is no persisted EmailJob for the given lead_id."""


# ---------------------------------------------------------------------------
# Item 5: deterministic personalization from Lead data
# ---------------------------------------------------------------------------

# Generic (never vertical-specific) dictionary terms that describe an
# *industry/technology*, not a company. A company_name that IS one of
# these, exactly, is almost certainly a mis-extraction (e.g. "AI" lifted
# from an "industries" field rather than a real employer name) -- a real
# company's brand name is essentially never identical to a bare category
# word like this. Deliberately conservative/short: only exact, whole-name
# matches are rejected, so a real company whose name merely *contains* one
# of these words (e.g. "Example Test SaaS Co") is left alone.
_GENERIC_COMPANY_TERMS = frozenset(
    {
        "ai", "ml", "iot", "vr", "ar", "nlp", "llm", "api", "erp", "crm",
        "saas", "paas", "iaas", "b2b", "b2c", "it", "hr", "pr", "seo",
        "tech", "technology", "technologies", "software", "solutions",
        "platform", "cloud", "data", "digital", "automation", "analytics",
        "startup", "company", "corporation", "inc", "llc",
    }
)

# Cue words that signal "the institution named nearby is where this person
# studies/studied", not their employer -- e.g. "junior MIS student at RIT".
# Broader than quality.py's _EDU_ENROLLMENT_CLAUSE_RE: that regex only
# fires when the institution's own name literally contains a word like
# "University"/"Institute"; this one catches the very common case where the
# institution is referred to only by an ambiguous short-form/abbreviation
# (e.g. "RIT") that reads exactly like a plausible company name on its own
# and would otherwise slip past every other company-name check.
_ENROLLMENT_CUE_RE = re.compile(
    r"\b(?:student|studying|studies|attending|enrolled|alumnus|alumna|alumni)\b",
    re.I,
)
_ENROLLMENT_WINDOW_CHARS = 60


def _is_enrollment_mention(name: str, evidence_text: str) -> bool:
    """True if `name` only appears in `evidence_text` right after an
    enrollment cue word (e.g. "student at RIT") -- i.e. it reads like the
    person's school, not their employer, even though the institution's own
    name gives no other textual hint of that (see module docstring above).
    """
    name = (name or "").strip()
    if not name or not evidence_text:
        return False
    low_text = evidence_text.lower()
    low_name = name.lower()
    name_re = re.compile(r"\b" + re.escape(low_name) + r"\b")
    for m in _ENROLLMENT_CUE_RE.finditer(low_text):
        window = low_text[m.end(): m.end() + _ENROLLMENT_WINDOW_CHARS]
        if name_re.search(window):
            return True
    return False


def is_confident_company(lead: Lead) -> bool:
    """True only if `lead.company_name` can be confidently presented as
    this person's employer in outreach copy.

    Deliberately conservative -- this governs whether a company gets
    *claimed* in an email, so it's better to say nothing than to guess
    wrong. Combines:
      1. quality.is_valid_company_name() / is_non_company_org() -- the same
         checks the LLM company-extraction path (llm.py) already applies,
         reused rather than duplicated.
      2. A short, generic-term stoplist (see _GENERIC_COMPANY_TERMS) that
         catches bare industry/tech words (e.g. "AI") is_valid_company_name
         alone doesn't reject, since they're technically well-formed
         "names".
      3. An enrollment-mention check (see _is_enrollment_mention) that
         catches an educational institution referred to only by an
         ambiguous short form (e.g. "RIT") that reads exactly like a
         plausible company name in isolation.
      4. A glued-own-name check: upstream extraction sometimes corrupts
         company_name by concatenating the lead's own name onto the real
         company (e.g. company_name="Hebbia George Sivulka" for a lead
         actually named George Sivulka, or "SaasRise Ryan Allis. Austin").
         A company name that embeds the person's own full name is never a
         genuine employer name, so it's rejected here too rather than
         presented as fact in an email.
    """
    name = (lead.company_name or "").strip()
    if not name:
        return False
    if not is_valid_company_name(name):
        return False
    if is_non_company_org(name):
        return False
    if name.lower() in _GENERIC_COMPANY_TERMS:
        return False
    full_name = (lead.full_name or "").strip()
    if full_name and len(full_name) >= 4 and full_name.lower() in name.lower():
        return False
    evidence_text = f"{lead.job_title} {lead.profile_summary}"
    if _is_enrollment_mention(name, evidence_text):
        return False
    return True


def display_person_name(name: str) -> str:
    """Normalize a name for use in a greeting/subject line.

    Never invents or "corrects" a name (e.g. an ALL-CAPS or oddly-spelled
    name is never guessed at as a different, "more normal" spelling) --
    this only adjusts *casing* for a more professional-looking greeting
    (e.g. "MCHAEL" -> "Mchael", not a guessed "Michael"). Returns "" for
    anything that doesn't look like a usable person name at all (reusing
    quality.is_valid_person_name), so callers can fall back to a safe
    generic greeting instead of blindly using unusable input.
    """
    name = (name or "").strip()
    if not name or not is_valid_person_name(name):
        return ""
    if name.isupper() or name.islower():
        return name.title()
    return name


# Common scraped-headline artifact: a profile's "title" field sometimes
# turns out to be the *entire* headline ("<Name> - <Company> - <Role
# descriptor>") rather than just the role, because the discovery source
# couldn't cleanly separate them. Stripping a leading "<Name> - " and/or
# "<Company> - " duplicate recovers just the role/descriptor part so it
# reads naturally in a sentence ("... your role as {role} at {company}"
# rather than repeating the person's own name and company back to them).
def clean_job_title(job_title: str, full_name: str, company_name: str) -> str:
    title = (job_title or "").strip()
    if not title:
        return ""
    for prefix_source in (full_name, company_name):
        prefix_source = (prefix_source or "").strip()
        if not prefix_source:
            continue
        prefix = f"{prefix_source} - "
        if title.lower().startswith(prefix.lower()):
            title = title[len(prefix):].strip()
    # Also strip a trailing "at <company>"/"of <company>" clause when it
    # names the same company that will be separately appended by
    # compose_personalization's "... at {company}" phrasing (e.g. job_title
    # "Founder & CEO at Hebbia" with company_name "Hebbia") -- otherwise the
    # company name is stated twice in one sentence ("...as Founder & CEO at
    # Hebbia at Hebbia.").
    company_source = (company_name or "").strip()
    if company_source:
        suffix_re = re.compile(
            r"\s+(?:at|of)\s+" + re.escape(company_source) + r"\s*$", re.I
        )
        title = suffix_re.sub("", title).strip()
    # A "title" that's still this long/sentence-like after stripping known
    # artifacts is more likely a whole headline or bio than a usable role
    # phrase -- safer to drop it than splice a run-on sentence into a
    # generated email.
    if not title or len(title) > 80 or title.count(" ") > 12:
        return ""
    return title


def format_industry_list(industry: str) -> str:
    """"SaaS; AI; Automation" -> "SaaS, AI and Automation" -- a natural-
    language rendering of the (semicolon/comma-separated) industry field,
    de-duplicated case-insensitively while preserving order. "" if empty."""
    parts = [p.strip() for p in re.split(r"[;,]", industry or "") if p.strip()]
    seen: set[str] = set()
    clean: list[str] = []
    for p in parts:
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        clean.append(p)
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    if len(clean) == 2:
        return f"{clean[0]} and {clean[1]}"
    return f"{', '.join(clean[:-1])}, and {clean[-1]}"


def primary_industry_term(industry: str) -> str:
    parts = [p.strip() for p in re.split(r"[;,]", industry or "") if p.strip()]
    return parts[0] if parts else ""


@dataclass
class PersonalizationResult:
    """Evidence-grounded personalization computed for one Lead. Every
    string here is built only from Lead fields that are themselves
    evidence (never invented) -- see compose_personalization()."""

    display_first_name: str
    company_used: str  # "" unless is_confident_company(lead) was True
    role_used: str  # cleaned job_title, "" if unusable/absent
    industries_used: str  # formatted industry list, "" if absent
    opening_line: str
    value_line: str
    subject_hook: str
    evidence_used: tuple[str, ...]  # e.g. ("role", "company") -- for tests/checks
    fallback: bool  # True iff no evidence at all was available


def compose_personalization(lead: Lead) -> PersonalizationResult:
    """Build every evidence-grounded, derived string used by
    DEFAULT_SUBJECT_TEMPLATE/DEFAULT_BODY_TEMPLATE (and available to any
    custom campaign template via {{opening_line}}/{{value_line}}/
    {{subject_hook}}). Pure function of `lead` -- no network, no LLM, fully
    deterministic, and never fabricates a company/product/fact beyond what
    is already present in the lead's own fields.
    """
    display_first = display_person_name(lead.first_name) or "there"
    confident_company = is_confident_company(lead)
    company = lead.company_name.strip() if confident_company else ""
    role = clean_job_title(lead.job_title, lead.full_name, lead.company_name)
    industries = format_industry_list(lead.industry)
    primary_industry = primary_industry_term(lead.industry)

    evidence_used: list[str] = []
    if company and role:
        opening = f"I noticed your role as {role} at {company}."
        evidence_used += ["role", "company"]
    elif company:
        opening = f"I came across {company} and wanted to reach out."
        evidence_used += ["company"]
    elif role and industries:
        opening = f"I noticed your work as {role} in {industries}."
        evidence_used += ["role", "industry"]
    elif role:
        opening = f"I noticed your work as {role}."
        evidence_used += ["role"]
    elif industries:
        opening = f"I came across your profile and noticed your work in {industries}."
        evidence_used += ["industry"]
    else:
        opening = "I came across your profile and wanted to reach out."

    if company:
        subject_hook = f"Quick question about {company}"
    elif primary_industry:
        subject_hook = f"Quick question about your {primary_industry} work"
    elif display_first != "there":
        subject_hook = f"Quick question, {display_first}"
    else:
        subject_hook = "Quick question"

    if industries:
        value_line = (
            f"I'd love to learn more about what you're building in "
            f"{industries} — would you be open to a quick call?"
        )
    else:
        value_line = "Would you be open to a quick call sometime this week?"

    return PersonalizationResult(
        display_first_name=display_first,
        company_used=company,
        role_used=role,
        industries_used=industries,
        opening_line=opening,
        value_line=value_line,
        subject_hook=subject_hook,
        evidence_used=tuple(evidence_used),
        fallback=not evidence_used,
    )


# ---------------------------------------------------------------------------
# Aug 2026: sharper, evidence-vs-hedged-hypothesis-vs-generic-aware,
# role-intelligent first-touch generation (translating the useful,
# non-deceptive parts of the "Gautam call" sales methodology into
# deterministic, evidence-grounded outreach copy).
#
# Deliberately built ALONGSIDE (not by modifying) compose_personalization()
# above: every existing test locked to that function's exact wording
# continues to pass unchanged, and this reuses its already-computed,
# evidence-grounded company/role/industry fields rather than re-deriving
# them differently.
# ---------------------------------------------------------------------------

EMAIL_1_INTRO = "EMAIL_1_INTRO"
EMAIL_2_FOLLOWUP = "EMAIL_2_FOLLOWUP"
EMAIL_3_EXPANSION = "EMAIL_3_EXPANSION"
EMAIL_4_CALL_TRANSITION = "EMAIL_4_CALL_TRANSITION"
EMAIL_STAGES = (EMAIL_1_INTRO, EMAIL_2_FOLLOWUP, EMAIL_3_EXPANSION, EMAIL_4_CALL_TRANSITION)

# Role-aware problem hypotheses (requirement C): NEVER asserted as fact --
# always framed as "teams like this often run into X", never "you have X
# problem". Deliberately industry-NEUTRAL wording (no "roadmap"/"codebase"/
# "engineering sprint" language) so this is a sane fallback for ANY
# industry (a boutique spa, a law firm, a manufacturer -- not just SaaS/
# tech), not just the tech-company examples this pipeline started with.
# For sharper, genuinely industry-specific phrasing, configure
# Campaign.pain_points (see select_pain_angle below) -- this dict is only
# the safety-net default when nothing more specific is configured.
# Multiple angles per band purely so two different leads with the same
# title don't receive byte-identical copy; selection is a stable hash of
# lead_id (see _stable_index), not randomness, so rendering stays
# deterministic.
_ROLE_PAIN_ANGLES: dict[str, list[tuple[str, str]]] = {
    "c_level": [
        ("scaling without cost creep", "scaling the business without operating costs growing just as fast"),
        ("staying focused while scaling", "keeping priorities clear once the team and the day-to-day both get busier"),
        ("turning growth into repeat business", "turning growth into predictable, repeat business rather than one-off wins"),
    ],
    "tech_exec": [
        ("systems keeping up with growth", "the tools and systems behind the scenes not quite keeping up as the business grows"),
        ("technology debt", "technology or process debt piling up faster than there's time to address it"),
        ("automation payoff", "figuring out where automation actually saves time versus where it just adds overhead"),
        ("reliability under load", "keeping everything running reliably while adding new things at the same time"),
    ],
    "product": [
        ("offering keeping pace", "the offering evolving more slowly than customers/clients would like"),
        ("uptake gap", "adoption or uptake lagging behind what was expected"),
        ("delivery gap", "a gap between what's promised and what customers actually experience"),
    ],
    "vp_director": [
        ("delivery bottlenecks", "timelines slipping as more things compete for the same team's time"),
        ("predictable output", "keeping a growing team's output predictable week to week"),
        ("process friction", "process or coordination friction that eats into time for the actual work"),
    ],
    "practitioner": [
        ("day-to-day friction", "day-to-day tooling or process friction slowing down the actual work"),
        ("manual work", "repetitive manual work that keeps eating into time for higher-value tasks"),
    ],
    "unknown": [
        ("operational friction", "operational friction that tends to show up once things start scaling"),
        ("manual work at scale", "manual work that starts competing with higher-value priorities as things grow"),
    ],
}


def _stable_index(key: str, length: int) -> int:
    """Deterministic (not random) selection so the same lead always renders
    the same copy (see test_render_email_deterministic-style expectations),
    while different leads still get some variety instead of byte-identical
    templates."""
    if length <= 0:
        return 0
    return (abs(hash(key)) if key else 0) % length


def role_pain_angle(job_title: str, lead_id: str) -> tuple[str, str, str]:
    """(band_used, short_label, problem_phrase) for one lead, from the
    generic, industry-neutral fallback pool -- see select_pain_angle() for
    the campaign-aware version that prefers a configured, industry-specific
    pain point when one matches. `band_used` is one of _ROLE_PAIN_ANGLES's
    keys, chosen from the title's seniority band (see
    qualification_scoring.seniority_band) with a "product" override when
    the title itself mentions product (requirement C's Product-leader
    bucket, which the shared seniority taxonomy doesn't otherwise carve
    out). `short_label` is a clean 2-4 word phrase safe to use in a subject
    line (never a truncated sentence fragment)."""
    title = job_title or ""
    if re.search(r"\bproduct\b", title, re.I):
        band = "product"
    else:
        band = seniority_band(title)
        if band not in _ROLE_PAIN_ANGLES:
            band = "unknown"
    angles = _ROLE_PAIN_ANGLES[band]
    label, phrase = angles[_stable_index(lead_id, len(angles))]
    return band, label, phrase


def select_pain_angle(campaign: Campaign | None, lead: Lead) -> tuple[str, str, str, str]:
    """(band_used, short_label, problem_phrase, source) -- `source` is
    "custom" when a campaign-configured Campaign.pain_points entry matched
    this lead's industry/keywords/role, or "generic" when falling back to
    the industry-neutral _ROLE_PAIN_ANGLES pool above.

    This is what makes the generator genuinely tailorable to ANY industry
    (beauty/wellness, legal, manufacturing, ...): a campaign targeting a
    specific vertical can supply its own real problem statements --
    {"industries": [...], "keywords": [...], "roles": [...], "label":
    "...", "phrase": "..."} -- and those are preferred whenever they match,
    over the generic tech-adjacent-by-default pool. Still never fabricates:
    a "custom" phrase is exactly what the campaign owner typed, and the
    generic pool is deliberately industry-neutral rather than guessing.
    """
    generic_band, generic_label, generic_phrase = role_pain_angle(lead.job_title, lead.lead_id)

    if campaign is None:
        return generic_band, generic_label, generic_phrase, "generic"

    candidates = campaign.pain_points
    if not candidates:
        return generic_band, generic_label, generic_phrase, "generic"

    lead_industry_terms = {
        t.strip().lower() for t in re.split(r"[;,]", lead.industry or "") if t.strip()
    }
    lead_text = f"{lead.job_title} {lead.profile_summary} {lead.industry}".lower()

    best: dict[str, Any] | None = None
    best_score = 0
    for pp in candidates:
        if not isinstance(pp, dict) or not (pp.get("phrase") or "").strip():
            continue
        score = 0
        for industry in pp.get("industries") or []:
            industry_l = str(industry).strip().lower()
            if industry_l and (industry_l in lead_industry_terms or industry_l in lead_text):
                score += 2
        for keyword in pp.get("keywords") or []:
            keyword_l = str(keyword).strip().lower()
            if keyword_l and keyword_l in lead_text:
                score += 1
        for role in pp.get("roles") or []:
            role_l = str(role).strip().lower()
            if role_l and (role_l == generic_band or role_l in (lead.job_title or "").lower()):
                score += 1
        if score > best_score:
            best_score = score
            best = pp

    if best is None:
        return generic_band, generic_label, generic_phrase, "generic"

    label = str(best.get("label") or best["phrase"][:40]).strip()
    phrase = str(best["phrase"]).strip()
    return generic_band, label, phrase, "custom"


# ---------------------------------------------------------------------------
# Case-study selection (requirement E): pick the most relevant VERIFIED
# case study for this lead, or None. Never fabricates -- a campaign with no
# case_studies configured always returns None here, and the caller must
# fall back to clearly-hedged hypothesis language (see _evidence_line).
# ---------------------------------------------------------------------------


def select_case_study(campaign: Campaign | None, lead: Lead) -> dict[str, Any] | None:
    if campaign is None:
        return None
    candidates = campaign.case_studies
    if not candidates:
        return None

    lead_industry_terms = {
        t.strip().lower() for t in re.split(r"[;,]", lead.industry or "") if t.strip()
    }
    lead_text = f"{lead.job_title} {lead.profile_summary} {lead.industry}".lower()

    best: dict[str, Any] | None = None
    best_score = 0
    for cs in candidates:
        if not isinstance(cs, dict) or not (cs.get("text") or "").strip():
            continue
        score = 0
        for industry in cs.get("industries") or []:
            industry_l = str(industry).strip().lower()
            if not industry_l:
                continue
            if industry_l in lead_industry_terms or industry_l in lead_text:
                score += 2
        for keyword in cs.get("keywords") or []:
            keyword_l = str(keyword).strip().lower()
            if keyword_l and keyword_l in lead_text:
                score += 1
        if score > best_score:
            best_score = score
            best = cs
    return best


# ---------------------------------------------------------------------------
# CTA selection (requirement F): low-friction by default; a direct call ask
# is only ever offered when there's real evidence (a verified case study
# AND at least role+one-more-signal grounding this specific lead) -- never
# forced when evidence is weak, and never the same CTA on every email.
# ---------------------------------------------------------------------------

_CTA_SOFT_POOL = [
    "Worth sending over the short case study?",
    "Is this something you're seeing on your side?",
    "Curious if this is relevant to your team.",
    "Happy to send the short example if useful.",
    "Would it be useful if I shared how we approached this?",
]
_CTA_CALL = "Would a quick call make sense to see if this is relevant?"


def select_cta(*, has_case_study: bool, evidence_strength: int, lead_id: str, stage: str) -> tuple[str, str]:
    """Returns (cta_text, cta_type). `evidence_strength` is the count of
    grounded signals used (role/industry/company) -- 0-3."""
    if stage == EMAIL_4_CALL_TRANSITION:
        return _CTA_CALL, "call"
    if has_case_study and evidence_strength >= 2 and _stable_index(lead_id, 3) == 0:
        return _CTA_CALL, "call"
    idx = _stable_index(lead_id + stage, len(_CTA_SOFT_POOL))
    return _CTA_SOFT_POOL[idx], "soft"


# ---------------------------------------------------------------------------
# Roll-up dataclass + composer
# ---------------------------------------------------------------------------


@dataclass
class OutreachPersonalizationResult:
    hook_line: str
    evidence_line: str
    cta_line: str
    problem_subject: str
    hook_type: str
    evidence_used: bool
    evidence_sources: tuple[str, ...]
    personalization_confidence: float
    cta_type: str
    stage: str
    pain_point_source: str = "generic"

    def metadata(self) -> dict[str, Any]:
        return {
            "hook_type": self.hook_type,
            "evidence_used": self.evidence_used,
            "evidence_sources": list(self.evidence_sources),
            "personalization_confidence": round(self.personalization_confidence, 3),
            "cta_type": self.cta_type,
            "stage": self.stage,
            "pain_point_source": self.pain_point_source,
        }


def compose_outreach_personalization(
    lead: Lead, campaign: Campaign | None = None, *, stage: str = EMAIL_1_INTRO
) -> OutreachPersonalizationResult:
    """Build the sharper hook/evidence/CTA structure (requirements A-K).

    Reuses compose_personalization(lead)'s already-evidence-grounded
    company/role/industry fields rather than re-deriving them -- so a
    company/role that compose_personalization() decided NOT to trust (e.g.
    RIT, or a bare "AI") never appears here either.
    """
    base = compose_personalization(lead)
    company = base.company_used
    role = base.role_used
    industries = base.industries_used
    primary_industry = primary_industry_term(lead.industry) if industries else ""

    band, pain_label, pain_phrase, pain_source = select_pain_angle(campaign, lead)

    evidence_signals = 0
    if role:
        evidence_signals += 1
    if primary_industry:
        evidence_signals += 1
    if company:
        evidence_signals += 1

    # --- hook_line (requirement A/C/D) -----------------------------------
    industry_phrase = primary_industry or "this space"
    if stage == EMAIL_2_FOLLOWUP:
        lead_in = f"Following up on my note about {industry_phrase} businesses and {pain_phrase}."
    elif stage == EMAIL_3_EXPANSION:
        lead_in = f"One more thing that often comes up alongside {pain_phrase}:"
    else:
        lead_in = f"Businesses in {industry_phrase} often run into {pain_phrase}."

    if company and role:
        hook_line = f"{lead_in} Given your role as {role} at {company}, I was curious whether that's something you're seeing as well."
        hook_type = "role_industry_company" if primary_industry else "role_company"
    elif company:
        hook_line = f"{lead_in} I was curious whether that's something {company} is running into as well."
        hook_type = "company_industry" if primary_industry else "company_only"
    elif role and primary_industry:
        hook_line = f"{lead_in} Given your work as {role}, I was curious whether that's something you're seeing."
        hook_type = "role_industry"
    elif role:
        hook_line = f"In {role} roles, {pain_phrase} tends to come up sooner or later."
        hook_type = "role_only"
    elif primary_industry:
        hook_line = lead_in
        hook_type = "industry_only"
    else:
        hook_line = "I came across your profile and wanted to reach out — happy to share something relevant if it's useful."
        hook_type = "generic_fallback"

    # --- evidence_line (requirement B/E) ----------------------------------
    case_study = select_case_study(campaign, lead)
    evidence_sources: list[str] = []
    if case_study:
        text = str(case_study.get("text", "")).strip()
        evidence_line = f"We recently worked on a similar problem — {text}"
        evidence_used = True
        evidence_sources.append(f"case_study:{text[:80]}")
    elif company:
        evidence_line = f"It looks like {company} may be dealing with something similar given the space you're in."
        evidence_used = False
    elif primary_industry:
        evidence_line = f"That's a pattern that comes up a lot for businesses in {primary_industry}."
        evidence_used = False
    else:
        evidence_line = "That's a pattern we've seen come up for a few different teams recently."
        evidence_used = False

    # --- CTA (requirement F) ----------------------------------------------
    cta_line, cta_type = select_cta(
        has_case_study=bool(case_study),
        evidence_strength=evidence_signals,
        lead_id=lead.lead_id,
        stage=stage,
    )

    # --- subject (requirement A: concise, problem/outcome-oriented) ------
    if primary_industry:
        problem_subject = f"{primary_industry} {pain_label}"
    elif company:
        problem_subject = f"{pain_label} at {company}"
    else:
        problem_subject = base.subject_hook

    confidence = min(1.0, (evidence_signals / 3.0) + (0.15 if case_study else 0.0))

    return OutreachPersonalizationResult(
        hook_line=hook_line,
        evidence_line=evidence_line,
        cta_line=cta_line,
        problem_subject=problem_subject,
        hook_type=hook_type,
        evidence_used=evidence_used,
        evidence_sources=tuple(evidence_sources),
        personalization_confidence=confidence,
        cta_type=cta_type,
        stage=stage,
        pain_point_source=pain_source,
    )


def personalization_context(lead: Lead) -> dict[str, str]:
    """The six supported template variables, mapped from a Lead's fields.

    Every value is a plain string (Lead fields are never None), so a
    missing optional field (e.g. no job_title) simply maps to "" -- exactly
    what render_template substitutes for a variable it's given, with no
    special-casing needed on either side.

    Deliberately left as a raw, unsanitized 1:1 mapping (unchanged since
    before evidence-grounding was added) -- see grounded_context() for the
    sanitized/derived version render_email() actually uses.
    """
    return {
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "company_name": lead.company_name,
        "job_title": lead.job_title,
        "location": lead.location,
        "industry": lead.industry,
    }


def grounded_context(lead: Lead, campaign: Campaign | None = None, *, stage: str = "EMAIL_1_INTRO") -> dict[str, str]:
    """personalization_context(), layered with evidence-grounded
    sanitization/derivation -- this is what render_email() actually renders
    templates against.

    Differences from the raw personalization_context():
      - company_name is blanked unless is_confident_company(lead) is True,
        so *no* campaign template (default or custom) can ever splice an
        unverified company into an email, even one written before this
        safeguard existed.
      - job_title is cleaned (see clean_job_title) to drop a duplicated
        name/company prefix that scraped headlines sometimes carry.
      - first_name is casing-normalized for the greeting (see
        display_person_name) -- never re-spelled, only re-cased.
      - opening_line / value_line / subject_hook are added: full,
        evidence-grounded sentences (see compose_personalization) that
        DEFAULT_SUBJECT_TEMPLATE/DEFAULT_BODY_TEMPLATE render directly,
        and any custom campaign template may opt into as well.
      - hook_line / evidence_line / cta_line / problem_subject (Aug 2026):
        the sharper, role/industry/company-aware, evidence-or-hedged-
        hypothesis 3-part structure (see compose_outreach_personalization)
        that SHARP_SUBJECT_TEMPLATE/SHARP_BODY_TEMPLATE render. Computed
        unconditionally (not just when a campaign uses those variables) so
        any template can opt in; when `campaign` is None (e.g. some legacy
        preview call sites) these fall back to campaign-agnostic content
        (no case-study lookup, since there's no campaign to look one up
        on).
    """
    ctx = personalization_context(lead)
    result = compose_personalization(lead)
    ctx["first_name"] = result.display_first_name
    ctx["company_name"] = result.company_used
    ctx["job_title"] = result.role_used
    ctx["opening_line"] = result.opening_line
    ctx["value_line"] = result.value_line
    ctx["subject_hook"] = result.subject_hook

    outreach = compose_outreach_personalization(lead, campaign, stage=stage)
    ctx["hook_line"] = outreach.hook_line
    ctx["evidence_line"] = outreach.evidence_line
    ctx["cta_line"] = outreach.cta_line
    ctx["problem_subject"] = outreach.problem_subject
    return ctx


@dataclass
class RenderedEmail:
    """A subject/body pair produced by rendering a Campaign against a Lead.
    Not itself persisted -- see EmailJob for the persisted form."""

    subject: str
    body: str


def render_email(campaign: Campaign, lead: Lead, *, stage: str = "EMAIL_1_INTRO") -> RenderedEmail:
    """Pure rendering: Campaign templates + one Lead's fields -> subject/body.

    Renders against grounded_context() (evidence-sanitized: unconfirmed
    company names blanked, job_title cleaned, name casing normalized, plus
    the derived opening_line/value_line/subject_hook AND the sharper
    hook_line/evidence_line/cta_line/problem_subject) rather than the raw
    personalization_context() -- see grounded_context()'s docstring. No
    persistence, no state-machine involvement, no side effects -- this is
    also exactly what preview_email() calls, so a preview and the eventually
    generated/persisted draft are always produced by the identical code
    path (item 8: preview reflects reality).

    `stage` (Aug 2026, default EMAIL_1_INTRO) only affects the derived
    hook_line/evidence_line/cta_line variables -- see EMAIL_STAGES and
    compose_outreach_personalization for what changes per stage. Campaigns
    using the older opening_line/value_line/subject_hook variables are
    completely unaffected by `stage`.
    """
    context = grounded_context(lead, campaign, stage=stage)
    return RenderedEmail(
        subject=render_template(campaign.subject_template, context),
        body=render_template(campaign.body_template, context),
    )


def preview_email(campaign: Campaign, lead: Lead) -> RenderedEmail:
    """Item 8: email preview functionality.

    Available for any Lead regardless of pipeline_status (a reviewer can
    preview what a campaign *would* render before any generation/commit
    step runs) -- unlike generate_email_for_lead, this never touches the
    store and never requires EMAIL_VALIDATED.
    """
    return render_email(campaign, lead)


# ---------------------------------------------------------------------------
# Quality safeguards -- run on every rendered draft *before* a Lead is ever
# allowed to reach EMAIL_GENERATED. These are deliberately separate from
# (and run after) grounded_context()/compose_personalization(): those build
# the best draft they can from available evidence; this is the final,
# independent check that the result is actually safe to send, so a bug or
# an unusual lead/template combination can't silently slip a bad draft
# through. Any issue found here diverts the lead to GENERATION_FAILED with
# the issue text as the reason (see generate_email_for_lead) instead of
# producing a bad email.
# ---------------------------------------------------------------------------

# Phrases that assert something an email should never claim unless it's
# grounded in the lead's own evidence -- funding, revenue, press, and
# similar specifics are exactly the kind of thing this pipeline must never
# invent (see module docstring / task requirements). This is a narrow,
# best-effort net: it can't catch every possible hallucination, but it
# reliably catches the clearest categories, and only fires when the phrase
# is NOT also present in the lead's own evidence text (i.e. it's fine to
# reference something the lead's own profile actually says).
_UNSUPPORTED_CLAIM_MARKERS = (
    "series a", "series b", "series c", "seed round", "funding round",
    "raised $", "raised a", "million in revenue", "billion in revenue",
    "acquired by", "acquisition of", "went public", "ipo", "recently launched",
    "just launched", "award-winning", "named to", "featured in forbes",
    "featured in techcrunch",
)

_PLACEHOLDER_RE = re.compile(r"\{\{\s*[a-zA-Z_][a-zA-Z0-9_]*\s*\}\}")


def _has_unsupported_claim(rendered: "RenderedEmail", lead: Lead) -> str:
    blob = f"{rendered.subject}\n{rendered.body}".lower()
    evidence = (
        f"{lead.job_title} {lead.profile_summary} {lead.industry} "
        f"{lead.company_name}"
    ).lower()
    for marker in _UNSUPPORTED_CLAIM_MARKERS:
        if marker in blob and marker not in evidence:
            return marker
    return ""


def assess_email_quality(lead: Lead, rendered: "RenderedEmail") -> list[str]:
    """Independent, final safety check on a rendered draft. Returns a list
    of human-readable issues (empty == passes). See module comment above
    for what this deliberately does and doesn't try to catch.
    """
    issues: list[str] = []
    blob = f"{rendered.subject}\n{rendered.body}"

    # 1. Recipient name is sane.
    if lead.first_name and not is_valid_person_name(lead.first_name):
        issues.append(f"recipient first_name {lead.first_name!r} does not look like a valid person name")

    # 2. Company name, if it appears anywhere in the draft, is one this
    #    lead's evidence actually confirms -- defense-in-depth on top of
    #    grounded_context() already blanking an unconfirmed company_name.
    #    Skipped for names on the generic-term stoplist (e.g. "AI"): those
    #    are rejected as a *company* precisely because they're ordinary
    #    industry/technology vocabulary, so their mere appearance elsewhere
    #    in grounded, industry-based copy (e.g. "...work in SaaS, AI and
    #    Automation") is expected and not itself suspicious. Word-boundary
    #    matched so a short name can't false-positive on a substring of an
    #    unrelated word.
    raw_company = (lead.company_name or "").strip()
    if (
        raw_company
        and not is_confident_company(lead)
        and raw_company.lower() not in _GENERIC_COMPANY_TERMS
        and re.search(r"\b" + re.escape(raw_company.lower()) + r"\b", blob.lower())
    ):
        issues.append(
            f"company name {raw_company!r} appears in the draft but could not be "
            "confidently established as this person's employer"
        )

    # 3. No unresolved {{variable}} placeholders leaked into the final text.
    if _PLACEHOLDER_RE.search(blob):
        issues.append("draft still contains an unresolved {{variable}} placeholder")

    # 4. No unsupported factual claims (funding/revenue/press/etc) beyond
    #    what the lead's own evidence supports.
    claim = _has_unsupported_claim(rendered, lead)
    if claim:
        issues.append(f"draft references {claim!r}, which isn't supported by this lead's evidence")

    # 5. Meaningful personalization: if this lead actually has usable
    #    evidence (confident company, a real job title, an industry, or a
    #    location), the draft must reflect at least one of them somewhere
    #    -- across subject AND body combined, so a plain subject next to a
    #    well-personalized body (a legitimate, common template style) is
    #    never penalized. A lead with NO evidence at all is exempt: the
    #    honest generic fallback compose_personalization() produces for
    #    that case is the intended, safe behavior, not a defect.
    evidence_tokens = [
        t for t in (
            lead.company_name.strip() if is_confident_company(lead) else "",
            clean_job_title(lead.job_title, lead.full_name, lead.company_name),
            primary_industry_term(lead.industry),
            lead.location.strip(),
        )
        if t
    ]
    if evidence_tokens:
        low_blob = blob.lower()
        if not any(tok.lower() in low_blob for tok in evidence_tokens):
            issues.append(
                "available evidence for this lead (role/company/industry/location) "
                "is not reflected anywhere in the drafted subject or body"
            )

    return issues


# ---------------------------------------------------------------------------
# Item 7: EmailJob -- persisted draft representation
# ---------------------------------------------------------------------------


@dataclass
class EmailJob:
    """The exact subject/body that will eventually be sent for one lead,
    plus its review state. Exactly one EmailJob exists per lead_id at a
    time (see UNIQUE(lead_id) in lead_store.py) -- generating again for the
    same lead overwrites this record rather than creating a new one, so
    "the current draft for this lead" is always an unambiguous lookup.
    """

    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    lead_id: str = ""
    campaign_id: str = ""
    subject: str = ""
    body: str = ""
    review_status: str = REVIEW_PENDING
    edited: bool = False
    rejection_reason: str = ""
    generated_at: str = ""
    reviewed_at: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    # Aug 2026: debugging/detail-view metadata -- hook_type, evidence_used,
    # evidence_sources, personalization_confidence, cta_type,
    # email_quality_score, stage. JSON-encoded; "{}" if not computed.
    # Deliberately not surfaced in the main email card UI (see
    # ProspectDetailOut / the Emails tab) -- available for a details/debug
    # view only. See PersonalizationResult.metadata() below.
    metadata_json: str = "{}"

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["edited"] = int(self.edited)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EmailJob":
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in known and v is not None}
        if "edited" in clean and not isinstance(clean["edited"], bool):
            clean["edited"] = bool(int(clean["edited"]))
        return cls(**clean)

    @property
    def metadata(self) -> dict[str, Any]:
        import json

        try:
            parsed = json.loads(self.metadata_json or "{}")
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}


def get_email_job(store: LeadStore, lead_id: str) -> EmailJob | None:
    row = store.get_email_job(lead_id)
    return EmailJob.from_dict(row) if row else None


def list_email_jobs(
    store: LeadStore, *, campaign_id: str | None = None, review_status: str | None = None
) -> list[EmailJob]:
    return [
        EmailJob.from_dict(row)
        for row in store.list_email_jobs(campaign_id=campaign_id, review_status=review_status)
    ]


# ---------------------------------------------------------------------------
# Item 6: EMAIL_VALIDATED -> EMAIL_GENERATED, only after successful
# rendering + persistence.
# ---------------------------------------------------------------------------


def compute_email_quality_score(rendered: "RenderedEmail", outreach: "OutreachPersonalizationResult", issues: list[str]) -> int:
    """0-100 (requirement J). Starts at 100 and only ever subtracts for
    concrete, checkable problems -- never a subjective "niceness" score.
    """
    score = 100
    score -= 30 * len(issues)  # any failed safeguard is a serious problem
    if outreach.hook_type == "generic_fallback":
        score -= 15  # honest, but the weakest personalization tier
    if not outreach.evidence_used and outreach.personalization_confidence < 0.34:
        score -= 10  # thin grounding beyond a bare hook
    body_len = len(rendered.subject) + len(rendered.body)
    if body_len > 700:
        score -= 10  # working against the "fits one mobile screen" requirement
    return max(0, min(100, score))


def _render_and_check(
    campaign: Campaign, lead: Lead, *, stage: str = EMAIL_1_INTRO
) -> tuple[RenderedEmail | None, str, "OutreachPersonalizationResult"]:
    """Render one lead's email and run every quality safeguard on it.

    Returns (rendered, "", outreach) on success, or (None, reason, outreach)
    if rendering produced nothing usable or the quality safeguards
    (assess_email_quality) rejected it -- `reason` is a human-readable
    string suitable for logging or surfacing to an operator, never just a
    generic failure marker. `outreach` (Aug 2026) is always returned so
    callers can persist its metadata regardless of success/failure.
    """
    rendered = render_email(campaign, lead, stage=stage)
    outreach = compose_outreach_personalization(lead, campaign, stage=stage)
    if not rendered.subject.strip() or not rendered.body.strip():
        return None, f"rendered email for lead_id={lead.lead_id!r} has an empty subject or body", outreach
    issues = assess_email_quality(lead, rendered)
    if issues:
        return None, "; ".join(issues), outreach
    return rendered, "", outreach


def generate_email_for_lead(
    store: LeadStore, lead: Lead, campaign: Campaign, *, stage: str = EMAIL_1_INTRO
) -> Lead:
    """Render + persist a personalized email for one EMAIL_VALIDATED lead,
    then transition it to EMAIL_GENERATED.

    Ordering matters (item 6 + item 7): the render happens first, its
    result is validated (non-empty subject/body, and now also the full
    assess_email_quality() safeguard suite -- see that function) and
    persisted as an EmailJob, and only *then* is the Lead's pipeline_status
    moved to EMAIL_GENERATED. A lead's status becoming EMAIL_GENERATED is
    therefore always backed by an actual persisted draft that has passed
    every quality safeguard; nothing can observe an EMAIL_GENERATED lead
    with no EmailJob for it, and nothing can observe a low-quality/
    ungrounded EmailJob at all. If rendering produces nothing usable, OR
    the quality safeguards reject the draft, the lead is diverted to
    GENERATION_FAILED instead (no partial/empty/low-quality draft is ever
    written), mirroring how email_discovery.py and email_validation.py
    divert to their own failure states rather than faking success. The
    specific reason is logged (see log.warning below) so a human running
    the pipeline can see *why* a lead failed, not just that it did.

    `stage` (Aug 2026, default EMAIL_1_INTRO): which of EMAIL_STAGES this
    draft is for -- affects only the hook_line/evidence_line/cta_line
    derived variables (see compose_outreach_personalization). No scheduler
    exists to auto-advance a lead through stages; a caller (e.g. a future
    follow-up job) chooses the stage explicitly, same as it already chooses
    the campaign.

    Raises InvalidStateTransition immediately (before any rendering or
    writes) if `lead` is not currently EMAIL_VALIDATED -- generation is
    only ever legal from that state.
    """
    validate_transition(lead.pipeline_status, PipelineStatus.EMAIL_GENERATED)

    rendered, reason, outreach = _render_and_check(campaign, lead, stage=stage)
    if rendered is None:
        log.warning("lead_id=%s campaign_id=%s GENERATION_FAILED: %s", lead.lead_id, campaign.campaign_id, reason)
        return store.transition(lead.lead_id, PipelineStatus.GENERATION_FAILED)

    quality_score = compute_email_quality_score(rendered, outreach, [])
    metadata = outreach.metadata()
    metadata["email_quality_score"] = quality_score

    now = utc_now_iso()
    existing = store.get_email_job(lead.lead_id)
    job = EmailJob(
        job_id=existing["job_id"] if existing else uuid.uuid4().hex,
        lead_id=lead.lead_id,
        campaign_id=campaign.campaign_id,
        subject=rendered.subject,
        body=rendered.body,
        review_status=REVIEW_PENDING,
        edited=False,
        rejection_reason="",
        generated_at=now,
        reviewed_at="",
        created_at=existing["created_at"] if existing else now,
        updated_at=now,
        metadata_json=json.dumps(metadata),
    )
    store.save_email_job(job.to_dict())

    lead.campaign_id = campaign.campaign_id
    store.save(lead)

    return store.transition(lead.lead_id, PipelineStatus.EMAIL_GENERATED)


def generate_pending_emails(store: LeadStore, campaign: Campaign, *, stage: str = EMAIL_1_INTRO) -> dict[str, Any]:
    """Process every Lead currently EMAIL_VALIDATED for `campaign`'s
    campaign_id.

    Resumable by construction, mirroring email_discovery.py /
    email_validation.py's bulk runners: only EMAIL_VALIDATED leads are
    pulled, so leads already moved on to EMAIL_GENERATED or
    GENERATION_FAILED are never reprocessed or re-rendered on a second run.

    Returns {"generated": int, "failed": int, "failures": [{"lead_id",
    "reason"}, ...]} -- `failures` gives the useful, per-lead reason for
    every GENERATION_FAILED (empty-render or a failed quality safeguard),
    not just a bare count.
    """
    generated = 0
    failed = 0
    failures: list[dict[str, str]] = []
    for lead in store.list_by_status(PipelineStatus.EMAIL_VALIDATED, campaign_id=campaign.campaign_id):
        _, reason, _outreach = _render_and_check(campaign, lead, stage=stage)
        updated = generate_email_for_lead(store, lead, campaign, stage=stage)
        if updated.status == PipelineStatus.EMAIL_GENERATED:
            generated += 1
        else:
            failed += 1
            failures.append({"lead_id": lead.lead_id, "reason": reason})
    return {"generated": generated, "failed": failed, "failures": failures}


# ---------------------------------------------------------------------------
# Items 8-9: preview (see above) / edit / approve / reject
# ---------------------------------------------------------------------------


def edit_email_job(store: LeadStore, lead_id: str, *, subject: str | None = None, body: str | None = None) -> EmailJob:
    """Overwrite the persisted draft's subject and/or body.

    Only legal while the lead is still EMAIL_GENERATED (i.e. under review,
    not yet approved or rejected) -- editing after a decision has been made
    would silently change what "APPROVED"/"REJECTED" referred to.
    """
    row = store.get_email_job(lead_id)
    if row is None:
        raise NoGeneratedEmail(f"No generated email for lead_id={lead_id!r}")

    lead = store.get(lead_id)
    if lead is None or lead.status != PipelineStatus.EMAIL_GENERATED:
        current = lead.status.value if lead is not None else "unknown"
        raise ValueError(
            f"Cannot edit draft for lead_id={lead_id!r}: lead is {current}, "
            "not EMAIL_GENERATED"
        )

    job = EmailJob.from_dict(row)
    if subject is not None:
        job.subject = subject
    if body is not None:
        job.body = body
    job.edited = True
    job.updated_at = utc_now_iso()
    store.save_email_job(job.to_dict())
    return job


def approve_email(store: LeadStore, lead_id: str) -> Lead:
    """EMAIL_GENERATED -> APPROVED for one lead, and mark its EmailJob approved."""
    row = store.get_email_job(lead_id)
    if row is None:
        raise NoGeneratedEmail(f"No generated email for lead_id={lead_id!r}")

    lead = store.transition(lead_id, PipelineStatus.APPROVED)

    now = utc_now_iso()
    job = EmailJob.from_dict(row)
    job.review_status = REVIEW_APPROVED
    job.reviewed_at = now
    job.updated_at = now
    store.save_email_job(job.to_dict())
    return lead


def reject_email(store: LeadStore, lead_id: str, *, reason: str = "") -> Lead:
    """EMAIL_GENERATED -> REJECTED for one lead, and mark its EmailJob rejected."""
    row = store.get_email_job(lead_id)
    if row is None:
        raise NoGeneratedEmail(f"No generated email for lead_id={lead_id!r}")

    lead = store.transition(lead_id, PipelineStatus.REJECTED)

    now = utc_now_iso()
    job = EmailJob.from_dict(row)
    job.review_status = REVIEW_REJECTED
    job.rejection_reason = reason
    job.reviewed_at = now
    job.updated_at = now
    store.save_email_job(job.to_dict())
    return lead


# ---------------------------------------------------------------------------
# Item 10: bulk approval/rejection
# ---------------------------------------------------------------------------


def bulk_approve(store: LeadStore, lead_ids: list[str]) -> dict[str, list]:
    """Approve every lead_id in `lead_ids`. Never aborts partway through --
    each lead is attempted independently, so one bad id (already decided,
    no draft, wrong state) doesn't block the rest of the batch."""
    approved: list[str] = []
    failed: list[dict[str, str]] = []
    for lead_id in lead_ids:
        try:
            approve_email(store, lead_id)
            approved.append(lead_id)
        except (NoGeneratedEmail, InvalidStateTransition, KeyError) as exc:
            failed.append({"lead_id": lead_id, "error": str(exc)})
    return {"approved": approved, "failed": failed}


def bulk_reject(store: LeadStore, lead_ids: list[str], *, reason: str = "") -> dict[str, list]:
    """Reject every lead_id in `lead_ids`. Same per-lead isolation as bulk_approve."""
    rejected: list[str] = []
    failed: list[dict[str, str]] = []
    for lead_id in lead_ids:
        try:
            reject_email(store, lead_id, reason=reason)
            rejected.append(lead_id)
        except (NoGeneratedEmail, InvalidStateTransition, KeyError) as exc:
            failed.append({"lead_id": lead_id, "error": str(exc)})
    return {"rejected": rejected, "failed": failed}


if __name__ == "__main__":
    import argparse

    from .campaign import load_campaign
    from .config import load_env

    ap = argparse.ArgumentParser(
        description="Day 7: generate personalized emails for EMAIL_VALIDATED leads in a campaign."
    )
    ap.add_argument("--db", default=None, help="Path to the LeadStore SQLite file (default: data/pipeline_state.db)")
    ap.add_argument("--campaign-id", required=True, help="Campaign to generate emails for")
    args = ap.parse_args()

    load_env()
    with (LeadStore(args.db) if args.db else LeadStore()) as store:
        campaign = load_campaign(store, args.campaign_id)
        if campaign is None:
            raise SystemExit(f"No campaign with campaign_id={args.campaign_id!r}")
        stats = generate_pending_emails(store, campaign)
        print(f"EMAIL_GENERATED:    {stats['generated']}")
        print(f"GENERATION_FAILED:  {stats['failed']}")
        for item in stats.get("failures", []):
            print(f"  lead_id={item['lead_id']}: {item['reason']}")
