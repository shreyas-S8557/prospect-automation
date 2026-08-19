"""Generic discovery-query generator, driven entirely by TargetConfig.

Replaces the old config.build_discovery_queries(), which had CPA titles and
CPA firm-type phrasing baked directly into the query strings. Every query
produced here follows the same shape the old CPA queries did (title/industry
+ location + "LinkedIn"), just parameterized instead of hard-coded, so the
existing discovery sources (ddgs, exa) keep working against it unchanged —
they only ever consume a flat list[str] of query strings.
"""

from __future__ import annotations

import random

from .config import US_CITIES, US_STATES
from .target_config import TargetConfig

# Cap on city x title expansion so a broad config (many titles, many cities)
# doesn't explode into tens of thousands of queries. Nationwide + per-state
# queries always run in full; per-city is the enrichment tier that gets capped.
MAX_ENRICHMENT_CITIES = 80


def build_queries(target: TargetConfig) -> list[str]:
    """Build the full discovery query set for a TargetConfig.

    Mirrors the structure of the old CPA-only builder:
      1. broad nationwide queries (qualified subject + "LinkedIn")
      2. per-state queries
      3. per-city queries (qualified subject, capped tier)
      4. explicit industry x title cross (kept for backward compatibility)
      5. city x title queries (capped, enrichment tier)
      6. keyword-augmented queries
      7. full keyword x industry x title combinations

    "Subject" = titles if given, else industries, else keywords, else a bare
    location search — so a config with only keywords (no titles) still
    produces a sensible query set instead of an empty list.

    Every tier below uses the *qualified* subject (see _qualified_subjects),
    not the bare subject. This matters a lot in practice: for a config like
    titles=["Founder","CEO","CTO"], industries=["SaaS"],
    keywords=["AI","automation"], a bare-subject query like "Founder United
    States LinkedIn" matches literally any founder in any industry — a
    physical therapy practice owner, a boutique retailer, an HR partner are
    all "a Founder/Partner ... in the United States". Since the broad
    nationwide/state/city tiers vastly outnumber a single dedicated
    "industry x title" tier (thousands of city queries vs. a handful of
    industry-tagged ones), leaving them unqualified means the vast majority
    of discovery queries carry no SaaS/AI signal at all, and the qualified
    yield ends up tiny no matter how good the downstream qualification logic
    is. Folding the industry/keyword phrase into the subject itself, for
    every tier, fixes that at the source.
    """
    subjects = _subjects(target)
    qualified_subjects = _qualified_subjects(target, subjects)
    locations = target.locations or ["United States"]
    is_us = any(_is_us_location(loc) for loc in locations)

    queries: list[str] = []

    # 1. Broad nationwide / per-location queries
    for loc in locations:
        for subject in qualified_subjects:
            queries.append(f"{subject} {loc} LinkedIn")

    # 2 & 3. US state/city expansion — only meaningful when targeting the US
    # broadly (matches old behaviour, which was nationwide-only).
    if is_us:
        for state in US_STATES:
            for subject in qualified_subjects:
                queries.append(f"{subject} {state} United States LinkedIn")
        for city in US_CITIES:
            for subject in qualified_subjects[:3]:  # keep this tier cheap
                queries.append(f"{subject} {city} United States LinkedIn")

    # 4. Per-title queries (nationwide), if titles were given separately from
    # what became the "subject" (e.g. subjects already includes titles, but
    # we still want the "title + industry" cross product below).
    if target.titles and target.industries:
        for title in target.titles:
            for industry in target.industries:
                for loc in locations:
                    queries.append(f"{industry} {title} {loc} LinkedIn")

    # 5. City x title enrichment (capped), mirrors the old CPA_TITLES x
    # US_CITIES[:80] tier — now also qualified, same reasoning as tiers 1-3.
    if is_us and target.titles:
        for city in US_CITIES[:MAX_ENRICHMENT_CITIES]:
            for subject in qualified_subjects:
                queries.append(f"{subject} {city} United States LinkedIn")

    # 6. Keyword-augmented queries — combine keywords with titles/industries
    # rather than searching keywords alone (keywords alone are usually too
    # generic to return LinkedIn people-profile results).
    if target.keywords:
        base_subjects = target.titles or target.industries or []
        for kw in target.keywords:
            for subject in base_subjects or [kw]:
                for loc in locations:
                    queries.append(f"{subject} {kw} {loc} LinkedIn")

    # 7. Full keyword x industry x title combinations — the tightest, most
    # targeted tier, matching queries like "AI SaaS Founder LinkedIn" or
    # "generative AI SaaS Founder LinkedIn". Every piece here comes straight
    # from the configured TargetConfig; nothing is hard-coded, so a config
    # of Fintech + blockchain produces "blockchain Fintech Founder LinkedIn"
    # instead, with no code change.
    if target.keywords and target.industries and target.titles:
        for kw in target.keywords:
            for industry in target.industries:
                for title in target.titles:
                    queries.append(f"{kw} {industry} {title} LinkedIn")
                    queries.append(f"{kw} startup {title} LinkedIn")

    return list(dict.fromkeys(q.strip() for q in queries if q.strip()))


def shuffled_queries_for(target: TargetConfig, seed: int | None = None) -> list[str]:
    queries = build_queries(target)
    rng = random.Random(seed if seed is not None else 42)
    rng.shuffle(queries)
    return queries


def _subjects(target: TargetConfig) -> list[str]:
    """What to search for, in priority order: titles > industries > keywords.

    Uses the synonym-expanded views (see TargetConfig.expanded_titles/
    expanded_industries/expanded_keywords) so a configured title_synonyms
    map (e.g. "Founder" -> ["Founder & CEO", "Co-Founder", "Founding
    Partner"]) actually broadens the query set, not just qualification.
    Equal to the plain titles/industries/keywords lists when no synonyms
    are configured, so this changes nothing for an existing config.
    """
    if target.titles:
        return target.expanded_titles
    if target.industries:
        return target.expanded_industries
    if target.keywords:
        return target.expanded_keywords
    return ["professional"]


def _qualifiers(target: TargetConfig) -> list[str]:
    """Short industry/keyword phrase(s) to fold into every subject so the
    broad nationwide/state/city tiers stay on-topic instead of matching a
    founder/CEO/partner in any industry whatsoever. Entirely config-driven:

    - both industries and keywords configured -> cross product, e.g.
      industries=["SaaS"], keywords=["AI","automation"]
      -> ["AI SaaS", "automation SaaS"]
    - only one of the two configured -> that dimension alone
    - neither configured -> [] (no qualifier available)
    """
    if target.industries and target.keywords:
        return [f"{kw} {ind}" for kw in target.expanded_keywords for ind in target.expanded_industries]
    if target.industries:
        return list(target.expanded_industries)
    if target.keywords:
        return list(target.expanded_keywords)
    return []


def _qualified_subjects(target: TargetConfig, subjects: list[str]) -> list[str]:
    """Fold _qualifiers() into every subject, e.g. "Founder" -> "AI SaaS
    Founder". Only applies when `subjects` actually came from `titles` (the
    common case for a people-search campaign) — when there are no titles,
    `_subjects()` already fell back to industries/keywords as the subject
    itself, and re-combining them with the same terms would just produce
    redundant phrases like "SaaS SaaS". Falls back to the bare subjects
    unqualified when no industries/keywords are configured at all, so a
    config that's titles-only still works exactly as before.
    """
    if not target.titles:
        return subjects
    qualifiers = _qualifiers(target)
    if not qualifiers:
        return subjects
    combined = [f"{q} {s}".strip() for q in qualifiers for s in subjects]
    return list(dict.fromkeys(combined))


def _is_us_location(location: str) -> bool:
    loc = (location or "").strip().lower()
    return loc in {"united states", "usa", "u.s.", "us"} or loc in {s.lower() for s in US_STATES}

