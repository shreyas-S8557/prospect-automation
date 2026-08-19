"""Clean us_angel_investors_unique.csv: drop SERP name/URL mismatches, collapse slug variants."""

from __future__ import annotations

import csv
import re
import shutil
from datetime import datetime
from pathlib import Path

import sys

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS))

from pipeline.models import FIELDNAMES  # noqa: E402
from pipeline.quality import linkedin_slug, merge_investor_row, normalize_linkedin  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "data" / "us_angel_investors_unique.csv"
OUT = ROOT / "data" / "us_angel_investors_unique.csv"
CLEAN_COPY = ROOT / "data" / "us_angel_investors_unique_clean.csv"

LI = re.compile(r"linkedin\.com/in/([A-Za-z0-9\-_%]+)", re.I)


def slug_of(url: str) -> str:
    m = LI.search(url or "")
    return m.group(1).lower().rstrip("/") if m else ""


def name_parts(name: str) -> list[str]:
    return re.findall(r"[a-z]+", (name or "").lower())


def name_matches_slug(name: str, slug: str) -> bool:
    """True when the display name plausibly belongs to this LinkedIn slug."""
    parts = name_parts(name)
    if not parts or not slug:
        return False
    compact = re.sub(r"[^a-z0-9]", "", slug.lower())
    long_parts = [p for p in parts if len(p) > 1]
    first = long_parts[0] if long_parts else parts[0]
    last = long_parts[-1] if long_parts else parts[-1]

    # first + last both present (adam + salomone in adamsalomone / adam-salomone)
    if len(first) > 1 and len(last) > 1 and first in compact and last in compact:
        return True
    # first initial + last (asalomone, avirani, aazelton)
    if len(last) > 1 and (parts[0][0] + last) in compact:
        return True
    # initials + last (A. Elizabeth Lindsey -> aelindsey)
    if len(parts) >= 2:
        initials_last = "".join(p[0] for p in parts[:-1]) + parts[-1]
        if len(parts[-1]) > 1 and initials_last in compact:
            return True
    # slug is essentially last name (+ optional trailing id with a digit)
    core = re.sub(r"[-_][a-z0-9]*\d[a-z0-9]*$", "", slug.lower())
    core_c = re.sub(r"[^a-z]", "", core)
    if len(last) > 2 and core_c == last:
        return True
    return False


def trailing_id(slug: str) -> str:
    m = re.search(r"[-_]([a-z0-9]*\d[a-z0-9]*)$", slug.lower())
    return m.group(1) if m else ""


def slug_core(slug: str) -> str:
    s = re.sub(r"[-_][a-z0-9]*\d[a-z0-9]*$", "", slug.lower())
    return re.sub(r"[^a-z]", "", s)


def same_person(slug_a: str, slug_b: str, name_a: str = "", name_b: str = "") -> bool:
    """Collapse obvious URL variants of one person; keep distinct people with same name."""
    if not slug_a or not slug_b:
        return False
    ca = re.sub(r"[^a-z0-9]", "", slug_a.lower())
    cb = re.sub(r"[^a-z0-9]", "", slug_b.lower())
    if ca == cb:
        return True
    core_a, core_b = slug_core(slug_a), slug_core(slug_b)
    id_a, id_b = trailing_id(slug_a), trailing_id(slug_b)
    if core_a and core_b and core_a == core_b:
        # Same core but different LinkedIn numeric/hex suffixes => different people
        if id_a and id_b and id_a != id_b:
            return False
        return True
    # one compact form contains the other (stevendorval / stevenfdorvalcfa)
    shorter, longer = sorted([ca, cb], key=len)
    if len(shorter) >= 8 and shorter in longer:
        return True
    # Same display name + shared last name inside both slugs
    # (adam salomone: asalomone vs adamsalomone; alan fisher: fisheralan vs alan-fisher)
    na = re.sub(r"\s+", " ", (name_a or "").strip().lower())
    nb = re.sub(r"\s+", " ", (name_b or "").strip().lower())
    if na and na == nb:
        parts = name_parts(na)
        long_parts = [p for p in parts if len(p) > 1]
        if long_parts:
            last = long_parts[-1]
            first = long_parts[0]
            if len(last) > 2 and last in ca and last in cb:
                # distinct people: both have different trailing ids
                if id_a and id_b and id_a != id_b and core_a == core_b:
                    return False
                # first name or its initial appears in both
                if first in ca and first in cb:
                    return True
                if first[0] in (ca[0],) and first[0] in (cb[0],) and last in ca and last in cb:
                    return True
                # reversed slug order (fisheralan)
                if first in ca and first in cb:
                    return True
                if last in ca and last in cb and (first[0] + last in ca or first[0] + last in cb or first in ca or first in cb):
                    return True
    return False


def clean_location(loc: str) -> str:
    loc = (loc or "").strip()
    if not loc:
        return ""
    low = loc.lower()
    if "linkedin" in low or len(loc) > 80:
        return ""
    if loc.count(" - ") >= 2 or loc.count("|") >= 2:
        return ""
    return loc


def clean_title(title: str) -> str:
    title = (title or "").strip()
    if not title:
        return ""
    if " - LinkedIn" in title:
        title = title.split(" - LinkedIn")[0].strip()
    if len(title) > 200:
        title = title[:200].rstrip()
    return title


def row_score(row: dict) -> tuple:
    """Prefer richer / cleaner rows when collapsing variants."""
    return (
        1 if row.get("email") else 0,
        1 if row.get("phone") else 0,
        1 if row.get("industries") else 0,
        len(row.get("summary") or ""),
        len(row.get("profile_title") or ""),
        1 if "-" in slug_of(row.get("linkedin_url", "")) else 0,
    )


def collapse_variants(rows: list[dict]) -> list[dict]:
    kept: list[dict] = []
    for row in sorted(rows, key=row_score, reverse=True):
        s = slug_of(row.get("linkedin_url", ""))
        name = row.get("name", "")
        merged_into = False
        for i, existing in enumerate(kept):
            if same_person(
                s,
                slug_of(existing.get("linkedin_url", "")),
                name,
                existing.get("name", ""),
            ):
                kept[i] = merge_investor_row(existing, row)  # type: ignore[arg-type]
                merged_into = True
                break
        if not merged_into:
            kept.append(row)
    return kept


def main() -> None:
    # Prefer the largest pre-clean backup (original dirty merge), not a later re-backup
    backups = sorted(
        SRC.parent.glob(f"{SRC.stem}_pre_clean_*.csv"),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    source = backups[0] if backups else SRC
    raw = list(csv.DictReader(source.open(encoding="utf-8", newline="")))
    print(f"Loaded {len(raw)} rows from {source.name}")

    matched: list[dict] = []
    dropped_mismatch = 0
    for r in raw:
        li = normalize_linkedin(r.get("linkedin_url", "") or "")
        s = linkedin_slug(li) or slug_of(r.get("linkedin_url", ""))
        if not s or not name_matches_slug(r.get("name", ""), s):
            dropped_mismatch += 1
            continue
        row = {f: (r.get(f) or "").strip() for f in FIELDNAMES}
        row["linkedin_url"] = li or f"https://www.linkedin.com/in/{s}"
        row["location"] = clean_location(row.get("location", ""))
        row["profile_title"] = clean_title(row.get("profile_title", ""))
        matched.append(row)

    print(f"Kept name~slug matches: {len(matched)} (dropped {dropped_mismatch} mismatches)")

    collapsed = collapse_variants(matched)
    print(f"After collapsing URL variants: {len(collapsed)} (merged {len(matched) - len(collapsed)})")

    # Final uniqueness by LinkedIn slug
    by_slug: dict[str, dict] = {}
    for row in collapsed:
        s = linkedin_slug(normalize_linkedin(row["linkedin_url"])) or slug_of(row["linkedin_url"])
        if not s:
            continue
        if s in by_slug:
            by_slug[s] = merge_investor_row(by_slug[s], row)  # type: ignore[arg-type]
        else:
            by_slug[s] = row

    final_rows = sorted(by_slug.values(), key=lambda r: (r.get("name") or "").lower())
    print(f"Final unique LinkedIn profiles: {len(final_rows)}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = SRC.with_name(f"{SRC.stem}_pre_clean_{stamp}{SRC.suffix}")
    shutil.copy2(SRC, backup)
    print(f"Backup -> {backup.name}")

    for dest in (OUT, CLEAN_COPY):
        with dest.open("w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=FIELDNAMES)
            w.writeheader()
            w.writerows(final_rows)
        print(f"Wrote {len(final_rows)} -> {dest}")

    # Sanity
    slugs = [
        linkedin_slug(normalize_linkedin(r["linkedin_url"])) or slug_of(r["linkedin_url"])
        for r in final_rows
    ]
    assert len(slugs) == len(set(slugs))
    still_bad = sum(1 for r in final_rows if not name_matches_slug(r["name"], slug_of(r["linkedin_url"])))
    print(f"Sanity: unique slugs OK; remaining name~slug mismatches: {still_bad}")


if __name__ == "__main__":
    main()
