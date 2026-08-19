"""Unit smoke tests for pipeline quality filters."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_SCRIPTS))

from pipeline.config import MEMORY_PATH, MEMORY_PATHS, ROOT  # noqa: E402
from pipeline.quality import (  # noqa: E402
    add_investor,
    load_dedup_memory,
    load_linkedin_slugs_from_csv,
    set_dedup_memory,
)
from pipeline.sources.ddgs_search import parse_search_hit  # noqa: E402


def test_parse_angel_hit():
    hit = {
        "href": "https://www.linkedin.com/in/jane-angel",
        "title": "Jane Doe | Angel Investor | San Francisco Bay Area",
        "body": "Angel investor focused on SaaS startups in San Francisco, California, United States.",
    }
    row = parse_search_hit(hit)
    assert row is not None
    assert row["source"] == "ddgs_search"
    assert "linkedin.com/in/jane-angel" in row["linkedin_url"]


def test_reject_vc_firm():
    hit = {
        "href": "https://www.linkedin.com/in/acme-vc",
        "title": "Acme Ventures | Venture Capital Firm",
        "body": "We are a venture capital firm investing globally from London, England.",
    }
    assert parse_search_hit(hit) is None


def test_reject_non_us():
    hit = {
        "href": "https://www.linkedin.com/in/uk-angel",
        "title": "Bob Smith | Angel Investor",
        "body": "Angel investor based in London, England, United Kingdom.",
    }
    assert parse_search_hit(hit) is None


def test_dedup_memory_blocks_known_slug():
    set_dedup_memory({"known-angel"})
    investors: dict = {}
    row = {
        "name": "Known Angel",
        "location": "San Francisco, CA",
        "linkedin_url": "https://www.linkedin.com/in/known-angel",
        "profile_title": "Angel Investor",
        "summary": "Angel investor in SaaS",
        "industries": "",
        "email": "",
        "phone": "",
        "source": "ddgs_search",
    }
    assert add_investor(investors, row) is False
    assert investors == {}
    set_dedup_memory(set())
    assert add_investor(investors, row) is True
    assert "known-angel" in investors


def test_memory_clean_csv_loads():
    path = MEMORY_PATH if MEMORY_PATH.exists() else ROOT / "data" / "us_angel_investors_unique_clean.csv"
    if not path.exists():
        return
    slugs = load_linkedin_slugs_from_csv(path)
    assert len(slugs) >= 1000
    union = load_dedup_memory(MEMORY_PATHS)
    assert len(union) == len(slugs)
    # A known clean slug should be blocked
    sample = next(iter(slugs))
    set_dedup_memory(union)
    assert add_investor(
        {},
        {
            "name": "X",
            "location": "US",
            "linkedin_url": f"https://www.linkedin.com/in/{sample}",
            "profile_title": "Angel Investor",
            "summary": "angel investor",
            "industries": "",
            "email": "",
            "phone": "",
            "source": "ddgs_search",
        },
    ) is False
    set_dedup_memory(set())


if __name__ == "__main__":
    test_parse_angel_hit()
    test_reject_vc_firm()
    test_reject_non_us()
    test_dedup_memory_blocks_known_slug()
    test_memory_clean_csv_loads()
    print("ok")
