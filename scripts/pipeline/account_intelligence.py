"""Account-level intelligence: group discovered contacts by company so the
system stops treating "10 people from 10 unrelated companies" the same as
"10 people from one high-value account" -- the "multiple points of contact
per account" strategy from the Aug 2026 sales-call requirements.

Purely additive and computed on-the-fly from existing Lead records (no new
tables, no change to the DISCOVERED/QUALIFIED pipeline state machine). A
"company" here is grouped by normalized company_name (company_domain is
almost never populated by current discovery sources -- see Lead.company_domain
comment in models.py -- so name is the only reliable grouping key available
today; this module is written so a domain-based key can slot in later
without changing its shape).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from .models import Lead, PipelineStatus
from .qualification_scoring import seniority_band as _seniority_band

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")

_DECISION_BANDS = {"c_level", "tech_exec", "vp_director"}


def _normalize_company_key(name: str) -> str:
    text = (name or "").strip().lower()
    text = _NON_ALNUM_RE.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_evidence(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


@dataclass
class AccountContact:
    lead_id: str
    full_name: str
    job_title: str
    pipeline_status: str
    seniority_band: str
    champion_likelihood: float
    champion_level: str
    decision_authority_signal: str
    role_relevance_signal: str
    is_decision_maker_candidate: bool
    is_potential_champion: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "lead_id": self.lead_id,
            "full_name": self.full_name,
            "job_title": self.job_title,
            "pipeline_status": self.pipeline_status,
            "seniority_band": self.seniority_band,
            "champion_likelihood": self.champion_likelihood,
            "champion_level": self.champion_level,
            "decision_authority_signal": self.decision_authority_signal,
            "role_relevance_signal": self.role_relevance_signal,
            "is_decision_maker_candidate": self.is_decision_maker_candidate,
            "is_potential_champion": self.is_potential_champion,
        }


@dataclass
class Account:
    company_key: str
    company_name: str
    company_domain: str = ""
    industry: str = ""
    company_size: str = ""
    contacts: list[AccountContact] = field(default_factory=list)

    @property
    def contact_count(self) -> int:
        return len(self.contacts)

    @property
    def senior_contacts(self) -> list[AccountContact]:
        return [c for c in self.contacts if c.seniority_band in _DECISION_BANDS]

    @property
    def practitioner_contacts(self) -> list[AccountContact]:
        return [c for c in self.contacts if c.seniority_band == "practitioner"]

    @property
    def potential_champions(self) -> list[AccountContact]:
        return sorted(
            (c for c in self.contacts if c.is_potential_champion),
            key=lambda c: c.champion_likelihood,
            reverse=True,
        )

    @property
    def decision_maker_candidates(self) -> list[AccountContact]:
        return [c for c in self.contacts if c.is_decision_maker_candidate]

    @property
    def has_relevant_signal(self) -> bool:
        """Any contact whose role_relevance or need evidence is at least
        weak_signal -- i.e. the account shows *some* evidenced connection to
        the target problem, not just a title match."""
        return any(c.role_relevance_signal != "unknown" for c in self.contacts)

    def best_contact_path(self) -> list[AccountContact]:
        """Ranked recommendation of who to talk to first, in order.

        Deliberately NOT "most senior first" (the whole point of the
        watermelon-effect / CTO-vs-AVP requirement): a champion with strong
        problem-proximity evidence outranks a senior title with none. Ties
        broken by champion_likelihood, then seniority as a last resort.
        """
        def sort_key(c: AccountContact) -> tuple:
            decision_rank = 1 if c.is_decision_maker_candidate else 0
            champion_rank = c.champion_likelihood
            relevance_rank = {"strong_signal": 2, "weak_signal": 1, "unknown": 0}.get(
                c.role_relevance_signal, 0
            )
            return (decision_rank, relevance_rank, champion_rank)

        return sorted(self.contacts, key=sort_key, reverse=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "company_key": self.company_key,
            "company_name": self.company_name,
            "company_domain": self.company_domain,
            "industry": self.industry,
            "company_size": self.company_size,
            "contact_count": self.contact_count,
            "senior_contact_count": len(self.senior_contacts),
            "practitioner_contact_count": len(self.practitioner_contacts),
            "potential_champion_count": len(self.potential_champions),
            "decision_maker_candidate_count": len(self.decision_maker_candidates),
            "has_relevant_signal": self.has_relevant_signal,
            "contacts": [c.to_dict() for c in self.contacts],
            "best_contact_path": [c.lead_id for c in self.best_contact_path()],
        }


def _lead_to_contact(lead: Lead) -> AccountContact:
    evidence = _parse_evidence(lead.qualification_evidence)
    band = _seniority_band(lead.job_title or "")
    champion = evidence.get("champion_likelihood") or {}
    champion_value = float(champion.get("value") or 0.0)
    champion_level = champion.get("level") or "unknown"
    bund = evidence.get("bund") or {}
    d_signal = (bund.get("D") or {}).get("value", "unknown")
    role_relevance = (evidence.get("role_relevance") or {}).get("value", "unknown")

    # Decision-maker CANDIDATE (never "confirmed decision-maker" -- see
    # qualification_scoring module docstring: nothing here can *confirm*
    # decision authority from public data alone). Deliberately gated on
    # strong_signal, NOT weak_signal: score_decision_authority() only
    # returns weak_signal for a senior title with zero ownership evidence
    # (see its own "title alone" branch) -- flagging that as a "candidate"
    # would be exactly the title-as-proof fallacy this whole feature exists
    # to avoid. strong_signal requires actual ownership language tied to
    # matched evidence, which is a real (if still imperfect) signal.
    is_decision_candidate = band in _DECISION_BANDS and d_signal == "strong_signal"
    is_potential_champion = champion_level in ("medium", "high")

    return AccountContact(
        lead_id=lead.lead_id,
        full_name=lead.full_name,
        job_title=lead.job_title,
        pipeline_status=lead.pipeline_status,
        seniority_band=band,
        champion_likelihood=champion_value,
        champion_level=champion_level,
        decision_authority_signal=d_signal,
        role_relevance_signal=role_relevance,
        is_decision_maker_candidate=is_decision_candidate,
        is_potential_champion=is_potential_champion,
    )


def group_leads_into_accounts(leads: list[Lead]) -> list[Account]:
    """Group a campaign's leads into Account records, keyed by normalized
    company_name. Leads with no company_name are grouped under a single
    '(unknown company)' bucket rather than dropped, so nothing discovered is
    silently hidden."""
    accounts: dict[str, Account] = {}
    for lead in leads:
        company_name = (lead.company_name or "").strip() or "(unknown company)"
        key = _normalize_company_key(company_name) or "(unknown company)"
        acct = accounts.get(key)
        if acct is None:
            acct = Account(
                company_key=key,
                company_name=company_name,
                company_domain=lead.company_domain or "",
                industry=lead.industry or "",
                company_size=lead.company_size or "",
            )
            accounts[key] = acct
        else:
            # Fill in blanks opportunistically from any contact that has them.
            acct.company_domain = acct.company_domain or (lead.company_domain or "")
            acct.industry = acct.industry or (lead.industry or "")
            acct.company_size = acct.company_size or (lead.company_size or "")
        acct.contacts.append(_lead_to_contact(lead))

    return sorted(accounts.values(), key=lambda a: a.contact_count, reverse=True)


# ---------------------------------------------------------------------------
# Account-level funnel (see requirement #6)
# ---------------------------------------------------------------------------

_ACCOUNT_FUNNEL_STAGES = [
    "accounts_discovered",
    "accounts_with_relevant_signal",
    "accounts_with_multiple_contacts",
    "accounts_with_potential_champion",
    "accounts_with_decision_maker_candidate",
    "qualified_accounts",
]


def account_funnel(accounts: list[Account]) -> dict[str, int]:
    """Mirrors the prospect funnel's shape but at the account level. An
    account is "qualified" here if it has BOTH a relevant signal AND either
    a potential champion or a decision-maker candidate -- i.e. there's an
    evidenced path in, not just a title match somewhere in the account."""
    discovered = len(accounts)
    with_signal = [a for a in accounts if a.has_relevant_signal]
    with_multi = [a for a in with_signal if a.contact_count > 1]
    with_champion = [a for a in with_signal if a.potential_champions]
    with_decision_maker = [a for a in with_signal if a.decision_maker_candidates]
    qualified = [a for a in with_signal if a.potential_champions or a.decision_maker_candidates]

    return {
        "accounts_discovered": discovered,
        "accounts_with_relevant_signal": len(with_signal),
        "accounts_with_multiple_contacts": len(with_multi),
        "accounts_with_potential_champion": len(with_champion),
        "accounts_with_decision_maker_candidate": len(with_decision_maker),
        "qualified_accounts": len(qualified),
    }
