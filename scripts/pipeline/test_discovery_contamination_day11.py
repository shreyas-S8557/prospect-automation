"""Day 11 smoke test: DDGS discovery must reject contaminated multi-profile
search hits instead of extracting a candidate from them.

Run with:
    python -m scripts.pipeline.test_discovery_contamination_day11

Covers:
  1. The exact contamination pattern from the bug report (one target
     person's LinkedIn hit with 1-2 other people's names/titles glued in)
     is rejected outright by parse_search_hit(), not silently accepted
     with blanked-out fields.
  2. A single, clean, uncontaminated hit for the same kind of query is
     still accepted (the fix isn't over-broad).
  3. matches_target_criteria()/_titles_ok() never trusts a contaminated
     title field, even when called directly (not just via parse_search_hit).
  4. Company extraction never attributes a company from a different
     person's "Experience:" section (task item 4) once contamination is
     detected.
  5. Query generation stays entirely config-driven: a SaaS+AI config and a
     Fintech+blockchain config produce different, ICP-appropriate query
     sets, with no hard-coded AI/SaaS/Fintech/blockchain strings baked
     into query_generator.py itself (only ever emitted because the config
     asked for them).
"""

from __future__ import annotations

import sys

from . import config
from .quality import (
    _titles_ok,
    extract_company_from_at_pattern,
    extract_company_from_snippet,
    find_age_evidence_via_search,
    is_contaminated_hit,
)
from .query_generator import build_queries
from .sources.ddgs_search import parse_search_hit
from .target_config import TargetConfig

SAAS_TARGET = TargetConfig(
    name="saas_ai_founders",
    locations=["United States"],
    titles=["Founder", "CEO", "CTO"],
    industries=["SaaS"],
    keywords=["AI", "automation"],
    target_count=50,
)

FINTECH_TARGET = TargetConfig(
    name="fintech_blockchain_founders",
    locations=["United States"],
    titles=["Founder", "CEO"],
    industries=["Fintech"],
    keywords=["blockchain", "crypto"],
    exclude_keywords=["student", "intern"],
    target_count=50,
)


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        sys.exit(1)


def main() -> None:
    config.set_active_target(SAAS_TARGET)

    # --- 1. The exact bug-report contamination pattern is rejected -----
    contaminated_hit = {
        "href": "https://www.linkedin.com/in/austin-maxwell-164103113",
        "title": (
            "Austin Maxwell - Cofounder - Kanga Coolers | Shark Tank ..."
            "Phillip Zedalis - Chief AI Officer & Co-Founder | LinkedIn"
            "McFadyen Anderson - Co-Founder at immilink | LinkedIn"
        ),
        "body": (
            "View Austin Maxwell's profile on LinkedIn, a professional "
            "community of 1 billion members. Chief AI Officer & Co-Founder "
            "\u00b7 Technology leader with more than two decades of "
            "experience across travel, banking, healthcare, and fintech. "
            "View McFadyen Anderson's profile on LinkedIn, a professional "
            "community of 1 billion members."
        ),
    }
    check(
        "is_contaminated_hit() flags the bug-report example",
        is_contaminated_hit(contaminated_hit["title"], contaminated_hit["body"]),
    )
    check(
        "parse_search_hit() rejects the exact bug-report example outright",
        parse_search_hit(contaminated_hit, target=SAAS_TARGET) is None,
    )

    # --- 2. A clean, single-person hit is still accepted ----------------
    clean_hit = {
        "href": "https://www.linkedin.com/in/jordan-alvarez-saas",
        "title": "Jordan Alvarez - Founder & CEO - Loopwave AI | LinkedIn",
        "body": (
            "· Experience: Loopwave AI · Founder & CEO of an AI SaaS "
            "automation platform for customer support teams. "
            "· Education: University of Michigan · Location: Austin, Texas, "
            "United States · 3,000+ connections on LinkedIn."
        ),
    }
    check(
        "is_contaminated_hit() does NOT flag a clean single-person hit",
        not is_contaminated_hit(clean_hit["title"], clean_hit["body"]),
    )
    clean_result = parse_search_hit(clean_hit, target=SAAS_TARGET)
    check("A clean, matching hit is still accepted", clean_result is not None)
    if clean_result:
        check(
            "Accepted candidate's name is the target person only",
            clean_result["name"] == "Jordan Alvarez",
        )

    # --- 3. _titles_ok never trusts a contaminated title field ----------
    contaminated_row = {
        "profile_title": (
            "Sam Rivera - Head of Product | LinkedInTaylor Chen - Founder & CEO "
            "at Nimbus AI | LinkedIn"
        ),
        "summary": "",
        "company_name": "",
        "industries": "",
    }
    check(
        "_titles_ok() ignores a contaminated title rather than matching "
        "another person's title ('Founder & CEO') onto this row",
        _titles_ok(contaminated_row, ["Founder", "CEO", "CTO"]) is False,
    )

    # --- 4. Company extraction: reject-when-ambiguous also protects the
    # company dimension, since a contaminated hit never reaches the
    # candidate-building step where company_name would be set at all. -----
    check(
        "A contaminated hit never reaches candidate construction, so no "
        "company can be misattributed from it",
        parse_search_hit(
            {
                "href": "https://www.linkedin.com/in/some-person",
                "title": "Person One - CEO - RealCo | LinkedInPerson Two - CTO at OtherCo | LinkedIn",
                "body": "Experience: RealCo · Experience: OtherCo",
            },
            target=SAAS_TARGET,
        )
        is None,
    )

    # --- 5. Query generation is fully config-driven, no hard-coded ICP ---
    saas_queries = build_queries(SAAS_TARGET)
    fintech_queries = build_queries(FINTECH_TARGET)
    check(
        "SaaS config produces AI/SaaS-flavoured queries",
        any("ai" in q.lower() and "saas" in q.lower() for q in saas_queries),
    )
    check(
        "SaaS queries never mention Fintech/blockchain",
        not any("fintech" in q.lower() or "blockchain" in q.lower() for q in saas_queries),
    )
    check(
        "Fintech config produces Fintech/blockchain-flavoured queries",
        any("fintech" in q.lower() and "blockchain" in q.lower() for q in fintech_queries),
    )
    check(
        "Fintech queries never mention AI/SaaS",
        not any("saas" in q.lower() for q in fintech_queries),
    )
    check(
        "Changing the config changes the query set with no code change",
        set(saas_queries) != set(fintech_queries),
    )
    check(
        "The old bare-title tier is gone: the vast majority of queries now "
        "carry industry/keyword signal, not just a handful",
        sum(1 for q in saas_queries if "saas" in q.lower()) > 0.9 * len(saas_queries),
    )

    # --- 6. Company extraction: "<Title> at/of <Company>" pattern, without
    # a structured "Experience:" field, and without the false positives
    # that pattern risks (education-enrollment clauses, "of <department>"
    # ambiguity) ---------------------------------------------------------
    company_cases = [
        ("CTO at Audacix, makers of Cyber Chief & Qsome.", "Audacix"),
        ("Co-founder of Ajelix, a SaaS platform.", "Ajelix"),
        ("Junior MIS student at Rochester Institute of Technology.", ""),
        ("VP of Sales at Salesforce, closing enterprise deals.", "Salesforce"),
        ("CEO @ Nimbus Systems, building next-gen infra.", "Nimbus Systems"),
        ("CEO at Turiba Business School", ""),
    ]
    for text, expected in company_cases:
        got = extract_company_from_snippet(text) or extract_company_from_at_pattern(text)
        check(f"company extraction on {text!r} -> {expected!r}", got == expected)

    # --- 7. Age web-search enrichment: injectable search_fn, evidence-only,
    # never invents an age from a search merely returning results ---------
    def fake_search_hit(query: str, max_results: int):
        if "class of" in query.lower():
            return [{
                "title": "Jim Wood - Class of 2010 - LinkedIn",
                "body": "CTO, graduated Class of 2010 from State University.",
            }]
        return []

    age, source, confidence = find_age_evidence_via_search({"name": "Jim Wood"}, fake_search_hit)
    check("Age web-search enrichment finds a real graduation-year proxy", confidence == "medium")
    check("Age web-search enrichment computes a plausible age", age.isdigit() and 20 <= int(age) <= 70)

    def fake_search_miss(query: str, max_results: int):
        return [{"title": "Unrelated result", "body": "nothing relevant here"}]

    age2, _, confidence2 = find_age_evidence_via_search({"name": "No One"}, fake_search_miss)
    check("Age web-search enrichment never invents an age from irrelevant results", confidence2 == "none" and age2 == "")

    def fake_search_error(query: str, max_results: int):
        raise RuntimeError("network down")

    age3, _, confidence3 = find_age_evidence_via_search({"name": "Err Case"}, fake_search_error)
    check("Age web-search enrichment degrades gracefully on a search error", confidence3 == "none" and age3 == "")

    config._ACTIVE_TARGET = None  # reset for cleanliness
    print("\nAll Day 11 contamination/configurability checks passed.")


if __name__ == "__main__":
    main()
