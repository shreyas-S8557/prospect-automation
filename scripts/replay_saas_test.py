"""Regenerate / verify the saas_ai_founders 50-record test by replaying the
previously-captured raw DDGS hit text (title + snippet + linkedin_url,
stored in data/saas_ai_founders_raw_stale_snapshot.csv, a snapshot of an
earlier live run) through the *current* discovery + qualification pipeline.

Why "replay" instead of a fresh live run: this sandbox has no network
access (egress is disabled; ddgs/openai can't even be installed here), so
scripts.pipeline.sources.ddgs_search cannot reach DDGS/Yahoo/Bing. The
existing raw snapshot already carries each candidate's full raw
profile_title/summary text exactly as it came back from the DDGS phase, so
replaying those same (title, body, linkedin_url) triples through
parse_search_hit() + qualify_row() is a faithful, inspectable way to prove
the fixed logic actually changes the outcome on real, previously-observed
noisy data — without fabricating new "search results".

Two-stage pipeline, matching the actual production code path
(sources.ddgs_search.parse_search_hit -> orchestrator's company_name/age
enrichment phases -> quality.qualify_row):

  Stage A - discovery / contamination gate (parse_search_hit with
  require_target_match=False): is this raw hit a single, structurally
  clean person profile at all (not glued from multiple LinkedIn results,
  has a resolvable name/location, isn't a company page)? This is what
  "number of discovered candidates" / "number of rejected candidates"
  means below, and is exactly what task item 1 (contamination) is about.

  Stage B - enrichment + qualification (quality.qualify_row, same function
  the orchestrator's "qualify" phase calls): does this clean candidate
  actually meet the configured titles/industries/keywords/locations/age?
  Only the free, deterministic half of enrichment (extract_company_from_
  snippet, extract_age_proxy) runs in this sandbox - the LLM half
  (extract_company_names_llm / extract_ages_llm, task item 6) needs a real
  OPENAI_API_KEY + network and is not exercised here. This means industry/
  keyword evidence that would normally come from LLM-researched company
  context is unavailable in this replay; only whatever the raw DDGS
  snippet itself states counts.

Usage:
    python3 scripts/replay_saas_test.py
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline import config as cfg  # noqa: E402
from pipeline.quality import (  # noqa: E402
    extract_age_proxy,
    extract_company_from_at_pattern,
    extract_company_from_snippet,
    is_non_company_org,
    qualify_row,
)
from pipeline.sources.ddgs_search import parse_search_hit  # noqa: E402
from pipeline.target_config import TargetConfig  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
INPUT_CSV = REPO_ROOT / "data" / "saas_ai_founders_raw_stale_snapshot.csv"
TARGET_CONFIG_PATH = REPO_ROOT / "data" / "target_configs" / "saas_founders.json"
OUTPUT_CSV = REPO_ROOT / "data" / "saas_ai_founders.csv"
OUTPUT_QUALIFIED_CSV = REPO_ROOT / "data" / "saas_ai_founders_qualified.csv"
REPORT_PATH = REPO_ROOT / "data" / "saas_ai_founders_replay_report.txt"

FIELDNAMES = [
    "name", "location", "linkedin_url", "profile_title", "summary",
    "industries", "email", "phone", "source", "company_name",
    "company_size", "age", "age_source", "age_confidence",
    "keyword_relevance", "keyword_evidence", "industry_evidence",
    "qualification_status", "qualification_reason",
]


def main() -> None:
    target = TargetConfig.from_json_file(TARGET_CONFIG_PATH)
    cfg.set_active_target(target)

    with INPUT_CSV.open(encoding="utf-8", newline="") as f:
        raw_rows = list(csv.DictReader(f))

    discovered_rows: list[dict] = []
    rejection_examples: list[str] = []

    for raw in raw_rows:
        hit = {
            "href": raw.get("linkedin_url", ""),
            "title": raw.get("profile_title", ""),
            "body": raw.get("summary", ""),
        }
        # Stage A: contamination / structural-cleanliness gate.
        candidate = parse_search_hit(hit, target=target, require_target_match=False)
        if candidate is None:
            if len(rejection_examples) < 15:
                rejection_examples.append(
                    f"{raw.get('name','(unknown)')!r} <- title={raw.get('profile_title','')[:90]!r}"
                )
            continue

        # Stage B setup: per-candidate enrichment (task 6). Deterministic
        # only in this sandbox - see module docstring.
        if not candidate.get("company_name"):
            company = extract_company_from_snippet(candidate.get("summary", ""))
            if not company:
                title_text = f"{candidate.get('profile_title','') or ''} {candidate.get('summary','') or ''}"
                company = extract_company_from_at_pattern(title_text)
            if company and not is_non_company_org(company):
                candidate["company_name"] = company
            else:
                candidate["company_name"] = raw.get("company_name", "") or ""

        age, source, confidence = extract_age_proxy(candidate)
        candidate["age"], candidate["age_source"], candidate["age_confidence"] = age, source, confidence

        discovered_rows.append(candidate)

    qualified = 0
    disqualified = 0
    keyword_confirmed = 0
    industry_confirmed = 0
    age_usable = 0
    disqualify_examples: list[str] = []

    for row in discovered_rows:
        ok, reason = qualify_row(row, target)
        row["qualification_status"] = "qualified" if ok else "disqualified"
        row["qualification_reason"] = reason
        if ok:
            qualified += 1
        else:
            disqualified += 1
            if len(disqualify_examples) < 15:
                disqualify_examples.append(f"{row.get('name','')!r}: {reason}")
        if row.get("keyword_relevance") == "strong":
            keyword_confirmed += 1
        if row.get("industry_evidence"):
            industry_confirmed += 1
        if str(row.get("age_confidence", "")).lower() not in ("", "none"):
            age_usable += 1

    with OUTPUT_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        for row in sorted(discovered_rows, key=lambda r: (r.get("name") or "").lower()):
            w.writerow({k: row.get(k, "") for k in FIELDNAMES})

    qualified_rows = [r for r in discovered_rows if r.get("qualification_status") == "qualified"]
    with OUTPUT_QUALIFIED_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        for row in sorted(qualified_rows, key=lambda r: (r.get("name") or "").lower()):
            w.writerow({k: row.get(k, "") for k in FIELDNAMES})

    rejected = len(raw_rows) - len(discovered_rows)

    lines = []
    lines.append("=== saas_ai_founders replay report (no live network in this sandbox) ===")
    lines.append(f"Input raw rows (previous stale 50-record output): {len(raw_rows)}")
    lines.append(
        f"Discovered candidates (single clean person, passed contamination/"
        f"name/location/company-page gate): {len(discovered_rows)}"
    )
    lines.append(f"Rejected at discovery (contaminated / not a person / no usable name or location): {rejected}")
    lines.append(f"Qualified (passed qualify_row against saas_founders.json): {qualified}")
    lines.append(f"Disqualified (clean candidate, but failed target criteria): {disqualified}")
    lines.append(f"With confirmed keyword relevance (AI/automation evidence): {keyword_confirmed}")
    lines.append(f"With confirmed industry evidence (SaaS): {industry_confirmed}")
    lines.append(f"With usable age evidence (confidence != none): {age_usable}")
    lines.append("")
    lines.append("-- Example rejection reasons (discovery/contamination stage) --")
    lines.extend(rejection_examples)
    lines.append("")
    lines.append("-- Example disqualification reasons (qualify_row stage) --")
    lines.extend(disqualify_examples)
    lines.append("")
    lines.append(
        "NOTE: no OPENAI_API_KEY / network in this sandbox, so the LLM half of "
        "per-candidate enrichment (extract_company_names_llm / extract_ages_llm, "
        "task item 6) did not run. Industry/keyword evidence above reflects only "
        "what the raw DDGS snippet itself states - in a networked run, the "
        "'company_name'/'qualify' phases would research thin candidates further "
        "before final qualification, which should raise the qualified count."
    )

    report = "\n".join(lines)
    REPORT_PATH.write_text(report + "\n", encoding="utf-8")
    print(report)


if __name__ == "__main__":
    main()
