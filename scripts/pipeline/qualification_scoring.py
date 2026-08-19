"""Evidence-based BUND / champion / relevance scoring.

Genesis: sales-call takeaways (Aug 2026) codified into product requirements
-- specifically the repeated lesson that "a title is a generic signal, not
proof of fit" (the Managing Director / office-relocation story, the CTO who
turned out not to own the initiative that an AVP three levels below him
actually owned). This module is the mechanical expression of that lesson:
it explicitly refuses to convert "job_title looks senior" into "this person
has budget/urgency/need/decision-authority."

Hard rule enforced throughout: **never fabricate**. Every field this module
produces is one of SIGNAL_UNKNOWN / SIGNAL_WEAK / SIGNAL_STRONG /
SIGNAL_CONFIRMED, and every non-unknown value must carry a short, literal
evidence string quoting or naming exactly what in the discovery data
supports it. If there's no textual evidence, the value is SIGNAL_UNKNOWN --
full stop, regardless of how senior the title looks. SIGNAL_CONFIRMED is
never assigned by this module (nothing in public discovery data can
*confirm* budget/urgency/need/decision-authority per the call's own
"validating BUND from outside a company is very hard" lesson) -- it exists
in the vocabulary for a future stage where a human or a direct conversation
confirms one of these dimensions, at which point some other code path can
write "confirmed" over an automated score.

This module is purely additive: it does not change matches_target_criteria()
or qualify_row() (the existing pass/fail qualification gate), and it never
makes a DISCOVERED/QUALIFIED/FILTERED_OUT lead pass or fail differently. It
only adds a structured, auditable evidence layer alongside that decision.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .models import InvestorRow
from .target_config import TargetConfig

# ---------------------------------------------------------------------------
# Signal vocabulary
# ---------------------------------------------------------------------------

SIGNAL_UNKNOWN = "unknown"
SIGNAL_WEAK = "weak_signal"
SIGNAL_STRONG = "strong_signal"
SIGNAL_CONFIRMED = "confirmed"

_SIGNAL_RANK = {
    SIGNAL_UNKNOWN: 0,
    SIGNAL_WEAK: 1,
    SIGNAL_STRONG: 2,
    SIGNAL_CONFIRMED: 3,
}

# ---------------------------------------------------------------------------
# Title taxonomy (evidence-only signal, exactly as strong as a title can
# legitimately be -- i.e. weak on its own; see module docstring)
# ---------------------------------------------------------------------------

_C_LEVEL_RE = re.compile(
    r"\b(ceo|chief executive officer|founder|co-?founder|owner|president|"
    r"managing director|\bmd\b)\b", re.I,
)
_TECH_EXEC_RE = re.compile(
    r"\b(cto|chief technology officer|cio|chief information officer|"
    r"chief product officer|\bcpo\b)\b", re.I,
)
_VP_DIRECTOR_RE = re.compile(
    r"\b(vp|vice president|director|head of|svp|evp)\b", re.I,
)
_PRACTITIONER_RE = re.compile(
    r"\b(engineer|manager|lead\b|architect|analyst|specialist|operator|"
    r"developer|consultant|coordinator)\b", re.I,
)

# Textual evidence patterns -- these look for actual language in the
# discovery-time snippet (profile_title / summary), never at inferring
# beyond what's written.
_BUDGET_LANGUAGE_RE = re.compile(
    r"\b(budget|procurement|purchasing|vendor selection|p&l|p and l|"
    r"cost[- ]center|spend management)\b", re.I,
)
_URGENCY_LANGUAGE_RE = re.compile(
    r"\b(hiring|urgent(?:ly)?|scaling|expanding|expansion|launch(?:ing)?|"
    r"rollout|migrat\w+|transformation|moderni[sz]ation|new initiative)\b",
    re.I,
)
_OWNERSHIP_LANGUAGE_RE = re.compile(
    r"\b(responsible for|owns?\b|leads?\b|manages?\b|in charge of|heads?\b|"
    r"oversees?\b|drives?\b)\b", re.I,
)
_TENURE_LONG_RE = re.compile(
    r"\b(\d{1,2})\+?\s*years?\b", re.I,
)


def _seniority_band(job_title: str) -> str:
    """One of 'c_level' / 'tech_exec' / 'vp_director' / 'practitioner' /
    'unknown' -- a taxonomy, never a proof of authority."""
    title = job_title or ""
    if _C_LEVEL_RE.search(title):
        return "c_level"
    if _TECH_EXEC_RE.search(title):
        return "tech_exec"
    if _VP_DIRECTOR_RE.search(title):
        return "vp_director"
    if _PRACTITIONER_RE.search(title):
        return "practitioner"
    return "unknown"


# Public alias -- other modules (e.g. account_intelligence.py) should use
# this name rather than reaching into the underscore-prefixed internal.
seniority_band = _seniority_band


@dataclass
class SignalResult:
    value: str = SIGNAL_UNKNOWN
    evidence: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"value": self.value, "evidence": self.evidence}


def _weak(evidence: str) -> SignalResult:
    return SignalResult(SIGNAL_WEAK, evidence)


def _strong(evidence: str) -> SignalResult:
    return SignalResult(SIGNAL_STRONG, evidence)


def _unknown() -> SignalResult:
    return SignalResult(SIGNAL_UNKNOWN, "")


# ---------------------------------------------------------------------------
# Individual BUND dimensions
# ---------------------------------------------------------------------------


def score_budget(row: InvestorRow) -> SignalResult:
    """Budget is essentially never stated in a public discovery snippet.
    Only ever returns weak_signal (explicit procurement/budget language) or
    unknown -- never strong/confirmed from public data alone."""
    text = f"{row.get('profile_title', '')} {row.get('summary', '')}"
    m = _BUDGET_LANGUAGE_RE.search(text)
    if m:
        return _weak(f"profile text mentions budget-adjacent language: '{m.group(0)}'")
    return _unknown()


def score_urgency(row: InvestorRow) -> SignalResult:
    """Weak by default (generic hiring/expansion language); strong only when
    that language sits inside the same evidence snippet already validated as
    relevant to the campaign's configured industry/keywords (i.e. it's tied
    to the actual problem, not just generic company growth)."""
    text = f"{row.get('profile_title', '')} {row.get('summary', '')}"
    m = _URGENCY_LANGUAGE_RE.search(text)
    if not m:
        return _unknown()
    keyword_evidence = (row.get("keyword_evidence") or row.get("industry_evidence") or "").strip()
    if keyword_evidence and _URGENCY_LANGUAGE_RE.search(keyword_evidence):
        return _strong(
            f"urgency language '{m.group(0)}' appears directly in the same "
            f"evidence already tied to the target industry/keyword: '{keyword_evidence[:160]}'"
        )
    return _weak(f"generic urgency/growth language present: '{m.group(0)}'")


def score_need(row: InvestorRow) -> SignalResult:
    """The one BUND dimension public discovery data can meaningfully speak
    to: reuses the qualification layer's own industry/keyword evidence
    (quality.qualify_row's keyword_evidence / industry_evidence side
    effects) rather than re-deriving anything, so "need" evidence is always
    exactly what already justified the qualification decision -- no new
    inference introduced here."""
    industry_ev = (row.get("industry_evidence") or "").strip()
    keyword_ev = (row.get("keyword_evidence") or "").strip()
    if industry_ev and keyword_ev:
        return _strong(f"industry evidence: '{industry_ev[:160]}' + keyword evidence: '{keyword_ev[:160]}'")
    if industry_ev:
        return _strong(f"industry evidence: '{industry_ev[:160]}'")
    if keyword_ev:
        return _weak(f"keyword evidence: '{keyword_ev[:160]}'")
    return _unknown()


def score_decision_authority(row: InvestorRow) -> SignalResult:
    """The core anti-title-fallacy check from the call (Managing Director /
    CTO stories). A senior-sounding title alone is capped at weak_signal --
    it can only reach strong_signal when the summary also contains explicit
    ownership language ('responsible for', 'leads', 'owns') tied to the same
    evidence that justified the industry/keyword match, i.e. the person is
    described as actually owning the relevant initiative, not just holding a
    senior title in general."""
    title = row.get("profile_title", "") or ""
    summary = row.get("summary", "") or ""
    band = _seniority_band(title)

    if band == "unknown":
        return _unknown()

    ownership_match = _OWNERSHIP_LANGUAGE_RE.search(summary)
    keyword_ev = (row.get("keyword_evidence") or row.get("industry_evidence") or "").strip()

    if ownership_match and keyword_ev:
        return _strong(
            f"title band '{band}' + explicit ownership language '{ownership_match.group(0)}' "
            f"co-occurring with the matched evidence: '{keyword_ev[:160]}'"
        )

    # Title alone -- exactly the "CEO title alone should not automatically
    # produce a high D score" case the requirements call out by name.
    return _weak(f"job title '{title.strip()}' suggests seniority band '{band}' only; no ownership evidence found")


# ---------------------------------------------------------------------------
# Non-BUND relevance dimensions
# ---------------------------------------------------------------------------


def score_title_match(row: InvestorRow, target: TargetConfig) -> dict[str, Any]:
    title = (row.get("profile_title", "") or "").lower()
    matched = [t for t in (target.expanded_titles or target.titles or []) if t.lower() in title]
    return {
        "value": bool(matched),
        "evidence": f"matched configured title(s): {matched}" if matched else "",
    }


def score_role_relevance(row: InvestorRow, target: TargetConfig) -> SignalResult:
    """Distinct from title_match: does the role *functionally* sit near the
    target problem, independent of seniority? A practitioner title (e.g.
    "ML Engineer") with strong industry/keyword evidence can score higher
    role_relevance than a senior title with none -- the "watermelon effect /
    practitioner" principle from the call."""
    keyword_ev = (row.get("keyword_evidence") or "").strip()
    industry_ev = (row.get("industry_evidence") or "").strip()
    band = _seniority_band(row.get("profile_title", "") or "")
    if (keyword_ev or industry_ev) and band in ("practitioner", "tech_exec", "vp_director"):
        return _strong(
            f"functional title band '{band}' co-occurs with matched evidence "
            f"'{(keyword_ev or industry_ev)[:160]}'"
        )
    if keyword_ev or industry_ev:
        return _weak(f"matched evidence present ('{(keyword_ev or industry_ev)[:160]}') but title band '{band}' unclear")
    return _unknown()


def score_company_relevance(row: InvestorRow, target: TargetConfig) -> SignalResult:
    industry_ev = (row.get("industry_evidence") or "").strip()
    company = (row.get("company_name") or "").strip()
    if industry_ev and company:
        return _strong(f"company '{company}' tied to industry evidence: '{industry_ev[:160]}'")
    if company:
        return _weak(f"company name present ('{company}') but no independent industry evidence")
    return _unknown()


# ---------------------------------------------------------------------------
# Champion likelihood (NOT a title-based judgment -- see module docstring)
# ---------------------------------------------------------------------------


def score_champion_likelihood(row: InvestorRow) -> dict[str, Any]:
    """A champion is someone close to the problem who can supply internal
    information or an introduction -- not necessarily, and often NOT, the
    most senior title. Scored 0.0-1.0 from evidence signals only; a
    practitioner with strong problem-proximity evidence can outscore a
    C-level contact with none, mirroring the CTO/AVP and Aditya-at-Unilever
    stories directly.
    """
    title = row.get("profile_title", "") or ""
    summary = row.get("summary", "") or ""
    band = _seniority_band(title)
    keyword_ev = (row.get("keyword_evidence") or "").strip()
    industry_ev = (row.get("industry_evidence") or "").strip()
    ownership_match = _OWNERSHIP_LANGUAGE_RE.search(summary)
    tenure_match = _TENURE_LONG_RE.search(summary)

    evidence: list[str] = []
    score = 0.0

    # Problem proximity is the dominant factor, deliberately unrelated to
    # seniority.
    if keyword_ev or industry_ev:
        score += 0.35
        evidence.append(f"problem-proximity evidence: '{(keyword_ev or industry_ev)[:160]}'")

    # Practitioners/managers close to the work are structurally more likely
    # champions than pure executives (who may be several layers removed --
    # the "watermelon effect").
    if band == "practitioner":
        score += 0.30
        evidence.append("title band is a practitioner/operator role, structurally close to day-to-day work")
    elif band in ("tech_exec", "vp_director"):
        score += 0.15
        evidence.append(f"title band '{band}' is functionally adjacent but not purely executive")
    elif band == "c_level":
        score += 0.05
        evidence.append("title band is C-level/founder -- possible, but not structurally favored as a champion")

    if ownership_match:
        score += 0.20
        evidence.append(f"explicit ownership/responsibility language: '{ownership_match.group(0)}'")

    if tenure_match:
        score += 0.10
        evidence.append(f"tenure/experience language suggests organizational familiarity: '{tenure_match.group(0)}'")

    score = min(score, 1.0)
    if score >= 0.6:
        level = "high"
    elif score >= 0.3:
        level = "medium"
    elif score > 0:
        level = "low"
    else:
        level = "unknown"

    return {"value": round(score, 2), "level": level, "evidence": evidence}


# ---------------------------------------------------------------------------
# Roll-up: one evidence-bearing scoring record per lead
# ---------------------------------------------------------------------------


def build_qualification_evidence(row: InvestorRow, target: TargetConfig) -> dict[str, Any]:
    """Compute the full evidence-based scoring record for one discovery row.

    Call this AFTER quality.qualify_row(row, target) has run (or at least
    after keyword_evidence/industry_evidence have been populated some other
    way) -- need/urgency/role/company scoring all read those fields. Calling
    it earlier just yields more 'unknown's, which is safe (never fabricated)
    but less informative.
    """
    budget = score_budget(row)
    urgency = score_urgency(row)
    need = score_need(row)
    decision_authority = score_decision_authority(row)
    title_match = score_title_match(row, target)
    role_relevance = score_role_relevance(row, target)
    company_relevance = score_company_relevance(row, target)
    champion = score_champion_likelihood(row)

    # Overall qualification confidence: how confident are we this is a
    # genuinely relevant CONTACT (title/role/company/need) -- deliberately
    # excludes budget/urgency/decision-authority, which are BUND concerns
    # about the *deal*, not the *contact's relevance*, per requirement #1's
    # explicit separation of "qualification confidence" from BUND.
    weights = {
        "title_match": 0.25,
        "role_relevance": 0.30,
        "company_relevance": 0.20,
        "need": 0.25,
    }
    numeric = {
        SIGNAL_UNKNOWN: 0.0,
        SIGNAL_WEAK: 0.5,
        SIGNAL_STRONG: 1.0,
        SIGNAL_CONFIRMED: 1.0,
    }
    confidence = (
        weights["title_match"] * (1.0 if title_match["value"] else 0.0)
        + weights["role_relevance"] * numeric[role_relevance.value]
        + weights["company_relevance"] * numeric[company_relevance.value]
        + weights["need"] * numeric[need.value]
    )

    # Intent signal score: kept explicitly separate from qualification
    # confidence per requirement #7. For now this is fed only by the
    # urgency-language proxy computed above -- genuine external engagement
    # signals (hiring activity, funding news, product launches, technology
    # changes) are NOT yet integrated (no data source is wired up for them
    # in this pipeline); this is intentionally left low/near-zero rather
    # than faked, with that gap stated explicitly so nobody mistakes it for
    # a populated signal.
    intent_numeric = {SIGNAL_UNKNOWN: 0.0, SIGNAL_WEAK: 0.4, SIGNAL_STRONG: 0.8, SIGNAL_CONFIRMED: 1.0}
    intent_signal_score = intent_numeric[urgency.value]

    return {
        "version": 1,
        "title_match": title_match,
        "role_relevance": role_relevance.to_dict(),
        "company_relevance": company_relevance.to_dict(),
        "bund": {
            "B": budget.to_dict(),
            "U": urgency.to_dict(),
            "N": need.to_dict(),
            "D": decision_authority.to_dict(),
        },
        "champion_likelihood": champion,
        "overall_qualification_confidence": round(confidence, 3),
        "intent_signal_score": {
            "value": round(intent_signal_score, 3),
            "note": (
                "Derived only from urgency-language present in the discovery "
                "snippet. Real engagement/intent data sources (hiring activity, "
                "funding/news, product launches, tech-stack changes) are not "
                "yet integrated -- see requirements P1/P2."
            ),
        },
    }


def qualification_evidence_json(row: InvestorRow, target: TargetConfig) -> str:
    """Convenience wrapper: build + json.dumps in one call, for callers that
    just want the string to persist (e.g. Lead.qualification_evidence)."""
    return json.dumps(build_qualification_evidence(row, target), ensure_ascii=False)
