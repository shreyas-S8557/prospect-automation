#!/usr/bin/env python3
"""
Collect LinkedIn profiles of US SaaS founders whose companies raised funding in 2025.

Primary source: Y Combinator 2025 batches (W25, X25, S25, F25) — each company received
YC seed funding in 2025. Filters to US-based B2B/SaaS software companies and scrapes
founder LinkedIn URLs from public YC company pages.

Output: data/us_saas_founders_2025.csv (target: 500 unique founder LinkedIn profiles)
"""

from __future__ import annotations

import csv
import html
import json
import re
import sys
import time
from pathlib import Path

import httpx
from tqdm import tqdm

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "us_saas_founders_2025.csv"

YC_OSS_BASE = "https://yc-oss.github.io/api"
YC_COMPANY_URL = "https://www.ycombinator.com/companies"
BATCHES_2025 = ["w25", "x25", "spring-2025", "summer-2025", "fall-2025"]
TARGET_COUNT = 500
DELAY = 0.35
TIMEOUT = 20.0

SAAS_INDUSTRY_KEYWORDS = {
    "b2b",
    "saas",
    "enterprise",
    "software",
    "developer tools",
    "productivity",
    "sales",
    "marketing",
    "fintech",
    "security",
    "infrastructure",
    "analytics",
    "legal",
    "hr",
    "compliance",
    "finance and accounting",
    "engineering, product and design",
}

NON_SAAS_SKIP = {
    "consumer",
    "industrials",
    "agriculture",
    "aviation and space",
    "healthcare",
    "health tech",
    "biotech",
    "hardware",
    "robotics",
    "drones",
    "defense",
}


def is_us_company(company: dict) -> bool:
    loc = (company.get("all_locations") or "").lower()
    regions = " ".join(company.get("regions") or []).lower() if isinstance(company.get("regions"), list) else ""
    if "united states" in loc or "united states" in regions or ", usa" in loc:
        return True
    us_cities = (
        "san francisco", "new york", "austin", "boston", "seattle", "los angeles",
        "miami", "chicago", "denver", "atlanta", "palo alto", "mountain view",
        "san diego", "portland", "nashville", "dallas", "houston", "phoenix",
        "washington", "dc", "philadelphia", "salt lake", "raleigh", "charlotte",
    )
    return any(c in loc for c in us_cities)


def is_saas_company(company: dict) -> bool:
    tags = [t.lower() for t in (company.get("tags") or [])]
    industries = [i.lower() for i in (company.get("industries") or [])]
    one_liner = (company.get("one_liner") or "").lower()
    desc = (company.get("long_description") or company.get("description") or "").lower()
    text = " ".join(tags + industries + [one_liner, desc])

    if "saas" in tags or "enterprise software" in tags:
        return True
    if any(k in text for k in ("saas", "b2b software", "enterprise software", "api platform", "workflow automation")):
        return True
    if any(ind in NON_SAAS_SKIP for ind in industries):
        # Allow B2B fintech/insurance SaaS
        if "b2b" not in industries and "fintech" not in industries:
            return False
    if "b2b" in industries:
        return True
    if any(kw in text for kw in SAAS_INDUSTRY_KEYWORDS):
        return True
    return False


def normalize_linkedin(url: str) -> str:
    if not url:
        return ""
    url = url.strip().split("?")[0].rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url.lstrip("/")
    return url


def fetch_json(client: httpx.Client, url: str):
    try:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        print(f"  warn: {url}: {exc}", file=sys.stderr)
        return None


def load_2025_companies(client: httpx.Client) -> list[dict]:
    all_data = fetch_json(client, f"{YC_OSS_BASE}/companies/all.json")
    if not all_data:
        raise SystemExit("Failed to fetch YC company list")

    batch_slugs: set[str] = set()
    for batch in BATCHES_2025:
        data = fetch_json(client, f"{YC_OSS_BASE}/batches/{batch}.json")
        if data:
            batch_slugs.update(c["slug"] for c in data)
            print(f"  {batch}: {len(data)} companies", file=sys.stderr)
        else:
            print(f"  warn: batch {batch} unavailable", file=sys.stderr)

    companies = [c for c in all_data if c.get("slug") in batch_slugs]
    filtered = [c for c in companies if is_us_company(c) and is_saas_company(c)]
    print(
        f"  Total 2025 YC: {len(companies)} | US SaaS/B2B filter: {len(filtered)}",
        file=sys.stderr,
    )
    return filtered


def scrape_founders(client: httpx.Client, slug: str) -> list[dict]:
    url = f"{YC_COMPANY_URL}/{slug}"
    try:
        resp = client.get(url)
        resp.raise_for_status()
    except Exception:
        return []

    match = re.search(r'data-page="(.*?)"', resp.text, re.DOTALL)
    if not match:
        return []

    try:
        page_data = json.loads(html.unescape(match.group(1)))
    except (json.JSONDecodeError, ValueError):
        return []

    company = page_data.get("props", {}).get("company", {})
    founders = []
    for f in company.get("founders", []):
        linkedin = normalize_linkedin(f.get("linkedin_url", ""))
        if linkedin and "linkedin.com/in/" in linkedin.lower():
            founders.append(
                {
                    "founder_name": f.get("full_name", ""),
                    "founder_title": f.get("title", ""),
                    "linkedin_url": linkedin,
                }
            )
    return founders


def main() -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    seen_linkedin: set[str] = set()

    if OUTPUT.exists():
        with OUTPUT.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                li = row.get("linkedin_url", "")
                if li and li not in seen_linkedin:
                    seen_linkedin.add(li)
                    rows.append(row)
        print(f"Loaded {len(rows)} existing founders from {OUTPUT}", file=sys.stderr)

    client = httpx.Client(
        timeout=TIMEOUT,
        headers={"User-Agent": "scrapegraph-saas-founder-collector/1.0"},
        follow_redirects=True,
    )

    print("Loading 2025 YC companies...", file=sys.stderr)
    companies = load_2025_companies(client)
    existing_companies = {r.get("company_name", "").lower() for r in rows}
    companies = [c for c in companies if c.get("name", "").lower() not in existing_companies]
    print(f"  Companies left to scrape: {len(companies)}", file=sys.stderr)

    for company in tqdm(companies, desc="Scraping founders"):
        if len(seen_linkedin) >= TARGET_COUNT:
            break

        founders = scrape_founders(client, company["slug"])
        for founder in founders:
            li = founder["linkedin_url"]
            if li in seen_linkedin:
                continue
            seen_linkedin.add(li)
            rows.append(
                {
                    "founder_name": founder["founder_name"],
                    "founder_title": founder["founder_title"],
                    "linkedin_url": li,
                    "company_name": company.get("name", ""),
                    "company_website": company.get("website", ""),
                    "company_batch": company.get("batch", ""),
                    "company_location": company.get("all_locations", ""),
                    "funding_year": "2025",
                    "funding_source": "Y Combinator",
                    "industries": "; ".join(company.get("industries") or []),
                    "tags": "; ".join(company.get("tags") or []),
                }
            )
            if len(seen_linkedin) >= TARGET_COUNT:
                break

        time.sleep(DELAY)

    client.close()

    fieldnames = [
        "founder_name",
        "founder_title",
        "linkedin_url",
        "company_name",
        "company_website",
        "company_batch",
        "company_location",
        "funding_year",
        "funding_source",
        "industries",
        "tags",
    ]
    with OUTPUT.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\nSaved {len(rows)} founders to {OUTPUT}", file=sys.stderr)
    if len(rows) < TARGET_COUNT:
        print(
            f"Note: collected {len(rows)}/{TARGET_COUNT}. "
            "Run again with supplemental sources or broaden filters.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
