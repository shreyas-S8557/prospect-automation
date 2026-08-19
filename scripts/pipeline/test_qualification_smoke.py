"""Day 3 milestone smoke test: the generic qualification/filtering layer.

Run with:
    python -m scripts.pipeline.test_qualification_smoke

This proves, without hitting the network or an LLM:
  1. matches_target_criteria() qualifies/rejects profiles purely from a
     TargetConfig — no CPA-specific code path is involved.
  2. A non-CPA campaign (SaaS founders) can actually pass the qualification
     layer, and CPA-specific phrasing is not required to do so.
  3. Irrelevant profiles (wrong title/industry, or a CPA profile evaluated
     against a SaaS campaign) are rejected.
  4. exclude_keywords reject a profile that would otherwise qualify.
  5. Age / age-proxy handling never rejects on missing data, never treats an
     age_confidence of "none"/"" as usable, and correctly filters when a
     properly labelled proxy is in range/out of range.
  6. Company size filtering behaves the same way as age: only enforced when
     data is actually present.
  7. The legacy CPA preset still qualifies real CPA profiles, and still
     rejects a SaaS founder profile.
  8. The same behaviour holds end-to-end through parse_search_hit(), i.e.
     the discovery-source integration is genuinely target-driven, not just
     the unit-level matches_target_criteria() function.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS))

from pipeline import config  # noqa: E402
from pipeline.quality import matches_target_criteria  # noqa: E402
from pipeline.sources.ddgs_search import parse_search_hit  # noqa: E402
from pipeline.target_config import CPA_PARTNER_PRESET, TargetConfig  # noqa: E402


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        sys.exit(1)


# --- fixtures ---------------------------------------------------------------

SAAS_TARGET = TargetConfig(
    name="saas_founders",
    locations=["United States"],
    titles=["Founder", "CEO", "CTO"],
    industries=["SaaS"],
    keywords=["AI", "automation"],
    exclude_keywords=["recruiter", "student"],
    age_min=22,
    age_max=45,
    target_count=500,
)

SAAS_FOUNDER_ROW = {
    "name": "Jane Doe",
    "location": "Austin, Texas, United States",
    "profile_title": "Jane Doe | Co-Founder & CEO at Streamline AI",
    "summary": (
        "Building an AI-powered automation platform for SaaS companies. "
        "Based in Austin, Texas, United States."
    ),
    "industries": "SaaS",
    "source": "ddgs_search",
}

IRRELEVANT_VC_ROW = {
    "name": "John Smith",
    "location": "New York, United States",
    "profile_title": "John Smith | Managing Partner at Big VC Fund",
    "summary": "Venture capital investor focused on early-stage fintech deals.",
    "industries": "Venture Capital",
    "source": "ddgs_search",
}

CPA_PARTNER_ROW = {
    "name": "Robert Chen",
    "location": "Chicago, Illinois, United States",
    "profile_title": "Robert Chen, CPA | Managing Partner at Chen & Associates CPAs",
    "summary": (
        "Managing partner at a CPA firm providing tax and audit services "
        "to small businesses in Chicago, Illinois, United States."
    ),
    "industries": "Accounting Firm",
    "source": "ddgs_search",
}

RECRUITER_ROW = {
    "name": "Alex Kim",
    "location": "United States",
    "profile_title": "Alex Kim | Founder & CEO, TalentFlow (Tech Recruiter)",
    "summary": (
        "Recruiter helping SaaS startups find engineering talent using an "
        "AI-powered automation platform."
    ),
    "industries": "SaaS",
    "source": "ddgs_search",
}


def main() -> None:
    # --- 1/2. Non-CPA campaign passes; no CPA phrasing required -----------
    check(
        "SaaS founder profile qualifies under the SaaS TargetConfig",
        matches_target_criteria(SAAS_FOUNDER_ROW, SAAS_TARGET) is True,
    )
    blob = (SAAS_FOUNDER_ROW["profile_title"] + SAAS_FOUNDER_ROW["summary"]).lower()
    check(
        "That match did not depend on any CPA-flavoured phrase",
        not any(p in blob for p in ("cpa", "certified public accountant", "accounting")),
    )

    # --- 3. Irrelevant profiles are rejected -------------------------------
    check(
        "Irrelevant VC-firm profile is rejected by the SaaS TargetConfig",
        matches_target_criteria(IRRELEVANT_VC_ROW, SAAS_TARGET) is False,
    )
    check(
        "A genuine CPA-partner profile is rejected by the SaaS TargetConfig",
        matches_target_criteria(CPA_PARTNER_ROW, SAAS_TARGET) is False,
    )

    # --- 4. exclude_keywords reject an otherwise-qualifying profile -------
    check(
        "Recruiter profile matches title/industry/keyword...",
        matches_target_criteria(
            {**RECRUITER_ROW, "profile_title": "Alex Kim | Founder & CEO, TalentFlow"},
            TargetConfig(titles=["Founder", "CEO"], industries=["SaaS"], keywords=["AI", "automation"]),
        )
        is True,
    )
    check(
        "...but is rejected once exclude_keywords=['recruiter'] applies",
        matches_target_criteria(RECRUITER_ROW, SAAS_TARGET) is False,
    )

    # --- 5. Age / age-proxy handling ---------------------------------------
    age_target = TargetConfig(name="age_test", titles=["Founder"], age_min=25, age_max=40, target_count=10)
    base_row = {"profile_title": "Founder", "summary": "Building a startup.", "industries": "", "location": "United States"}

    check(
        "No age data at all -> not rejected (unknown, not excluded)",
        matches_target_criteria(dict(base_row), age_target) is True,
    )
    check(
        "age present but age_confidence='none' -> proxy not trusted, not rejected",
        matches_target_criteria(
            {**base_row, "age": "22", "age_confidence": "none", "age_source": "rough_guess"},
            age_target,
        )
        is True,
    )
    check(
        "age present with age_confidence='' (unset) -> also not trusted",
        matches_target_criteria({**base_row, "age": "22", "age_confidence": ""}, age_target) is True,
    )
    check(
        "properly labelled proxy IN range -> qualifies",
        matches_target_criteria(
            {
                **base_row,
                "age": "30",
                "age_confidence": "medium",
                "age_source": "estimated from stated graduation year (proxy)",
            },
            age_target,
        )
        is True,
    )
    check(
        "properly labelled proxy OUT OF range -> rejected",
        matches_target_criteria(
            {
                **base_row,
                "age": "55",
                "age_confidence": "high",
                "age_source": "estimated from stated graduation year (proxy)",
            },
            age_target,
        )
        is False,
    )
    check(
        "unparseable age string -> treated as no data, not rejected",
        matches_target_criteria(
            {**base_row, "age": "unknown", "age_confidence": "high", "age_source": "x"}, age_target
        )
        is True,
    )

    # --- 6. Company size: only enforced when data is present ---------------
    size_target = TargetConfig(
        name="size_test", titles=["Founder"], company_size_min=50, company_size_max=500, target_count=10
    )
    check(
        "No company_size data -> not rejected",
        matches_target_criteria(dict(base_row), size_target) is True,
    )
    check(
        "company_size within range -> qualifies",
        matches_target_criteria({**base_row, "company_size": "120"}, size_target) is True,
    )
    check(
        "company_size out of range -> rejected",
        matches_target_criteria({**base_row, "company_size": "5"}, size_target) is False,
    )
    check(
        "company_size as a free-text range ('51-200 employees') is parsed",
        matches_target_criteria({**base_row, "company_size": "51-200 employees"}, size_target) is True,
    )

    # --- 7. Legacy CPA preset still works -----------------------------------
    check(
        "Genuine CPA-partner profile still qualifies under CPA_PARTNER_PRESET",
        matches_target_criteria(CPA_PARTNER_ROW, CPA_PARTNER_PRESET) is True,
    )
    check(
        "SaaS founder profile does NOT qualify under CPA_PARTNER_PRESET",
        matches_target_criteria(SAAS_FOUNDER_ROW, CPA_PARTNER_PRESET) is False,
    )

    # --- 8. End-to-end through parse_search_hit() (discovery-source layer) -
    saas_hit = {
        "href": "https://www.linkedin.com/in/jane-saas-founder",
        "title": SAAS_FOUNDER_ROW["profile_title"],
        "body": SAAS_FOUNDER_ROW["summary"],
    }
    cpa_hit = {
        "href": "https://www.linkedin.com/in/robert-chen-cpa",
        "title": CPA_PARTNER_ROW["profile_title"],
        "body": CPA_PARTNER_ROW["summary"],
    }
    check(
        "parse_search_hit(saas_hit, target=SAAS_TARGET) returns a row",
        parse_search_hit(saas_hit, target=SAAS_TARGET) is not None,
    )
    check(
        "parse_search_hit(saas_hit, target=CPA_PARTNER_PRESET) rejects it",
        parse_search_hit(saas_hit, target=CPA_PARTNER_PRESET) is None,
    )
    check(
        "parse_search_hit(cpa_hit, target=CPA_PARTNER_PRESET) returns a row (legacy)",
        parse_search_hit(cpa_hit, target=CPA_PARTNER_PRESET) is not None,
    )
    check(
        "parse_search_hit(cpa_hit, target=SAAS_TARGET) rejects it",
        parse_search_hit(cpa_hit, target=SAAS_TARGET) is None,
    )

    # Same thing again, but through the process-wide active target instead
    # of an explicit argument, proving the zero-arg call sites (as used by
    # run_ddgs_phase) pick up whatever campaign is active.
    config.set_active_target(SAAS_TARGET)
    check(
        "Active target set to SaaS -> parse_search_hit(saas_hit) with no target= arg passes",
        parse_search_hit(saas_hit) is not None,
    )
    check(
        "Active target set to SaaS -> parse_search_hit(cpa_hit) with no target= arg is rejected",
        parse_search_hit(cpa_hit) is None,
    )
    config._ACTIVE_TARGET = None  # reset for cleanliness / other tests

    print("\nAll Day 3 qualification-layer checks passed.")


if __name__ == "__main__":
    main()
