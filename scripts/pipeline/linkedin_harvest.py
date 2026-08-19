"""Extract LinkedIn /in/ profiles from page text without calling LinkedIn."""

from __future__ import annotations

import re

from .models import InvestorRow
from .quality import (
    extract_email,
    extract_location,
    extract_phone,
    is_valid_person_name,
    normalize_linkedin,
)

MD_LINK_RE = re.compile(
    r"\[([^\]]{2,80})\]\((https?://(?:www\.)?linkedin\.com/in/[^\s\)\"]+)\)",
    re.I,
)
BARE_LINK_RE = re.compile(
    r"(https?://(?:www\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+/?)",
    re.I,
)
NAME_NEAR_RE = re.compile(
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z.'\-]+){1,3}).{0,80}linkedin\.com/in/",
    re.I | re.S,
)


def harvest_linkedin_from_text(
    text: str,
    *,
    source: str,
    seed_name: str,
) -> list[InvestorRow]:
    """Pull /in/ URLs out of markdown/HTML. Never hits linkedin.com."""
    if not text:
        return []

    rows: list[InvestorRow] = []
    seen: set[str] = set()

    for name, url in MD_LINK_RE.findall(text):
        row = _row_from_hit(url, name=name, context=text, source=source, seed_name=seed_name)
        if row and row["linkedin_url"] not in seen:
            seen.add(row["linkedin_url"])
            rows.append(row)

    for url in BARE_LINK_RE.findall(text):
        li = normalize_linkedin(url)
        if not li or li in seen:
            continue
        name = _guess_name_near_url(text, url)
        row = _row_from_hit(url, name=name, context=text, source=source, seed_name=seed_name)
        if row and row["linkedin_url"] not in seen:
            seen.add(row["linkedin_url"])
            rows.append(row)

    return rows


def _guess_name_near_url(text: str, url: str) -> str:
    idx = text.lower().find(url.lower()[:40])
    window = text[max(0, idx - 120) : idx + 40] if idx >= 0 else text[:200]
    m = NAME_NEAR_RE.search(window)
    if m:
        return m.group(1).strip()
    slug = url.rstrip("/").split("/")[-1].replace("-", " ").replace("%20", " ")
    return " ".join(p.capitalize() for p in slug.split()[:4])


def _row_from_hit(
    url: str,
    *,
    name: str,
    context: str,
    source: str,
    seed_name: str,
) -> InvestorRow | None:
    li = normalize_linkedin(url)
    if not li:
        return None
    name = (name or "").strip()
    name = re.sub(r"\s+", " ", name)
    if not is_valid_person_name(name):
        name = _guess_name_near_url(context, url)
    if not is_valid_person_name(name):
        return None

    title = f"Angel Investor | {seed_name}"
    summary = (
        f"Angel investor listed on {seed_name}. "
        f"LinkedIn profile: {li}"
    )
    loc = extract_location(context, title) or "United States"
    return {
        "name": name,
        "location": loc,
        "linkedin_url": li,
        "profile_title": title[:500],
        "summary": summary[:2000],
        "industries": "",
        "email": extract_email(context),
        "phone": extract_phone(context),
        "source": source,
    }
