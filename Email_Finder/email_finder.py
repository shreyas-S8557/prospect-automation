#!/usr/bin/env python3
"""
Layer 0: Email pattern finder (guesses candidate emails from name + company).

Sits BEFORE verify_mx.js / verify_smtp.js in the pipeline:

    email_finder.py  -->  verify_mx.js  -->  verify_smtp.js
    (guess emails)        (drop dead        (confirm mailbox
                            domains)          exists via RCPT TO)

This script does NOT confirm anything is real — it only generates plausible
candidates using common professional email conventions, then hands them off
to the existing MX/SMTP layers to actually verify. Zero dependencies, pure
Python standard library (csv, json, re, argparse) to match the Node scripts'
zero-dependency style.

Usage:
    python email_finder.py contacts.csv                     # -> emails.json
    python email_finder.py contacts.csv -o candidates.json
    python email_finder.py contacts.csv --domains domains.json
    python email_finder.py contacts.csv --domain acmecpa.com
    python email_finder.py contacts.json --name-key name --company-key company

Input formats:
    CSV  — needs at least a name column (default: "name") and, ideally, a
           company column. Company resolution checks, in order:
             1. --company-key COLUMN, if given
             2. "company_name"   (canonical column from scripts/pipeline —
                                   already a bare, LLM-extracted brand name
                                   with legal suffixes like LLC/Inc. stripped)
             3. "company", "firm", "organization"
             4. an " at <Company>" pattern parsed out of profile_title/summary
    JSON — array of objects with the same keys.

Output:
    JSON array of guessed emails, e.g. ["john.smith@acmecpa.com", ...]
    — directly usable as input to verify_mx.js.

    A companion CSV (same basename + "_candidates.csv") is also written,
    listing name / company / resolved domain / all candidate emails per
    person, for auditing which guesses came from where.

Domain resolution, in priority order:
    1. --domain FLAG           (use this domain for every row)
    2. --domains domains.json  ({"Company Name": "company.com", ...} override map)
    3. explicit company/domain column in the input, if it already looks like a domain
    4. guessed from the company name (slugified + ".com")

Guessed domains (#4) are unreliable — always prefer supplying a domains.json
override map built from each firm's actual website when you have it.
"""

import argparse
import csv
import json
import os
import re
import sys

DEFAULT_PATTERNS = [
    "{first}.{last}",
    "{first}{last}",
    "{first}_{last}",
    "{f}{last}",
    "{first}",
    "{last}.{first}",
    "{last}{first}",
    "{f}.{last}",
    "{first}{l}",
    "{last}",
]

NAME_SUFFIXES = {
    "jr", "sr", "ii", "iii", "iv", "cpa", "cfa", "cfp", "esq", "phd", "mba",
}

COMPANY_STOPWORDS = {
    "the", "of", "and", "a", "an",
}

COMPANY_LEGAL_SUFFIXES = {
    "llc", "llp", "pllc", "pc", "pa", "inc", "incorporated", "corp",
    "corporation", "co", "cpas", "cpa", "associates", "assoc",
}


def slugify_domain(company: str) -> str:
    """Best-effort, unreliable guess: 'Smith & Jones CPAs LLP' -> 'smithjones.com'."""
    company = re.sub(r"[’']", "", company.lower())
    company = re.sub(r"[^a-z0-9\s&]", " ", company)
    company = company.replace("&", " and ")
    words = [w for w in company.split() if w not in COMPANY_STOPWORDS]
    # Drop trailing legal-entity words (LLP, CPAs, Inc, etc.)
    while words and words[-1] in COMPANY_LEGAL_SUFFIXES:
        words.pop()
    slug = "".join(words) or "company"
    return f"{slug}.com"


def looks_like_domain(value: str) -> bool:
    return bool(re.match(r"^[a-z0-9.-]+\.[a-z]{2,}$", (value or "").strip().lower()))


def extract_company_from_title(title: str) -> str:
    """'Managing Partner at Smith & Jones CPAs' -> 'Smith & Jones CPAs'."""
    if not title:
        return ""
    m = re.search(r"\bat\s+(.+)$", title, flags=re.IGNORECASE)
    return m.group(1).strip(" .,-") if m else ""


def split_name(full_name: str):
    """'John A. Smith Jr., CPA' -> ('john', 'smith')."""
    if not full_name:
        return "", ""
    name = re.sub(r",.*$", "", full_name)  # drop everything after a comma (credentials)
    parts = [p.strip(".") for p in name.split() if p.strip(".")]
    parts = [p for p in parts if p.lower().strip(".") not in NAME_SUFFIXES]
    if not parts:
        return "", ""
    first = parts[0].lower()
    last = parts[-1].lower() if len(parts) > 1 else ""
    # strip anything non-alpha (hyphenated names kept as-is, apostrophes dropped)
    first = re.sub(r"[^a-z\-]", "", first)
    last = re.sub(r"[^a-z\-]", "", last)
    return first, last


def build_candidates(first: str, last: str, domain: str, patterns):
    if not first or not domain:
        return []
    f, l = first[:1], last[:1]
    seen, out = set(), []
    for pat in patterns:
        try:
            local = pat.format(first=first, last=last, f=f, l=l)
        except (KeyError, IndexError):
            continue
        if not local or (not last and "{last}" in pat):
            continue
        email = f"{local}@{domain}"
        if email not in seen:
            seen.add(email)
            out.append(email)
    return out


def load_rows(path: str):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".json":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else [data]
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def resolve_company_and_domain(row, args, domain_overrides):
    company = ""
    if args.company_key and row.get(args.company_key):
        company = str(row[args.company_key]).strip()
    if not company:
        # "company_name" is the canonical, LLM-extracted column produced by the
        # scraper pipeline (scripts/pipeline) — already a bare brand name with
        # legal suffixes stripped, so it's preferred over the looser
        # "company"/"firm"/"organization" fallbacks and the title/summary
        # regex parse below.
        for key in ("company_name", "company", "firm", "organization"):
            if row.get(key):
                company = str(row[key]).strip()
                break
    if not company and row.get("profile_title"):
        company = extract_company_from_title(str(row["profile_title"]))
    if not company and row.get("summary"):
        company = extract_company_from_title(str(row["summary"]))

    if args.domain:
        return company, args.domain.lower().strip()

    if company:
        for key, dom in domain_overrides.items():
            if key.lower().strip() == company.lower().strip():
                return company, dom.lower().strip()

    if row.get("domain") and looks_like_domain(row["domain"]):
        return company, row["domain"].lower().strip()
    if company and looks_like_domain(company):
        return company, company.lower().strip()

    if not company:
        return company, ""
    return company, slugify_domain(company)


def main():
    ap = argparse.ArgumentParser(
        description="Guess candidate professional emails from name + company "
        "(Layer 0, feeds into verify_mx.js / verify_smtp.js)."
    )
    ap.add_argument("input", help="CSV or JSON file with name (+ company) columns")
    ap.add_argument("-o", "--output", default=None, help="Output JSON path (default: emails.json)")
    ap.add_argument("--name-key", default="name", help="Column/key holding the full name (default: name)")
    ap.add_argument(
        "--company-key",
        default=None,
        help="Column/key holding the company name (default: auto-detect — "
        "tries company_name, company, firm, organization, then falls back to "
        "parsing profile_title/summary)",
    )
    ap.add_argument("--domain", default=None, help="Use this single domain for every row")
    ap.add_argument("--domains", default=None, help="JSON file mapping {\"Company Name\": \"domain.com\"}")
    ap.add_argument(
        "--patterns",
        default=None,
        help="Comma-separated pattern list, e.g. '{first}.{last},{f}{last}'. "
        "Placeholders: {first} {last} {f} {l}",
    )
    args = ap.parse_args()

    patterns = DEFAULT_PATTERNS
    if args.patterns:
        patterns = [p.strip() for p in args.patterns.split(",") if p.strip()]

    domain_overrides = {}
    if args.domains:
        with open(args.domains, "r", encoding="utf-8") as f:
            domain_overrides = json.load(f)

    rows = load_rows(args.input)
    if not rows:
        print("No rows found in input.", file=sys.stderr)
        sys.exit(1)

    all_emails = []
    audit_rows = []
    no_domain = 0

    for row in rows:
        full_name = str(row.get(args.name_key, "")).strip()
        first, last = split_name(full_name)
        company, domain = resolve_company_and_domain(row, args, domain_overrides)

        if not domain:
            no_domain += 1
            audit_rows.append(
                {"name": full_name, "company": company, "domain": "", "candidates": ""}
            )
            continue

        candidates = build_candidates(first, last, domain, patterns)
        all_emails.extend(candidates)
        audit_rows.append(
            {
                "name": full_name,
                "company": company,
                "domain": domain,
                "candidates": "; ".join(candidates),
            }
        )

    # De-dupe while preserving order
    seen = set()
    unique_emails = []
    for e in all_emails:
        if e not in seen:
            seen.add(e)
            unique_emails.append(e)

    output_path = args.output or "emails.json"
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(unique_emails, f, indent=2)

    audit_path = os.path.splitext(output_path)[0] + "_candidates.csv"
    with open(audit_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "company", "domain", "candidates"])
        writer.writeheader()
        writer.writerows(audit_rows)

    print(f"Rows processed: {len(rows)}")
    print(f"Rows skipped (no company/domain resolvable): {no_domain}")
    print(f"Candidate emails generated: {len(unique_emails)}")
    print(f"-> {output_path}  (feed this into: node verify_mx.js {output_path})")
    print(f"-> {audit_path}  (per-person audit trail: which domain/pattern produced each guess)")

    if no_domain:
        print(
            "\nTip: rows without a resolvable domain were skipped. Supply a "
            "--domains domains.json override map (built from each firm's real "
            "website) for accuracy — the slugified guess is a fallback, not "
            "a verified domain.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
