"""Quality filters, normalization, and CPA-partner store helpers."""

from __future__ import annotations

import csv
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import get_active_target
from .models import DISCOVERY_SOURCES, DIRECTORY_SOURCES, FIELDNAMES, InvestorRow
from .target_config import TargetConfig

# LinkedIn slugs already present in baseline memory sheets (compare-only).
_DEDUP_MEMORY: set[str] = set()

_XLSX_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}

NON_US_LINKEDIN_SUBDOMAINS = (
    "uk.linkedin.com", "in.linkedin.com", "ca.linkedin.com", "de.linkedin.com",
    "fr.linkedin.com", "il.linkedin.com", "sg.linkedin.com", "au.linkedin.com",
    "br.linkedin.com", "mx.linkedin.com", "jp.linkedin.com", "kr.linkedin.com",
    "cn.linkedin.com", "nl.linkedin.com", "it.linkedin.com", "es.linkedin.com",
)

NON_US_TEXT_MARKERS = (
    "united kingdom", "(gb)", " england", " scotland", " wales", " london, england",
    "greater london", "toronto, ontario", "canada (ca)", "(ca)", "vancouver",
    "mumbai", "bangalore", "delhi, india", "india (in)", "berlin", "munich",
    "paris, france", "singapore (sg)", "sydney, australia", "melbourne, australia",
)

US_LOCATION_HINTS = (
    "united states", "usa", "u.s.", "(us)", ", us", "california", "texas",
    "new york", "san francisco", "boston", "austin", "seattle", "chicago",
    "miami", "los angeles", "denver", "atlanta", "philadelphia", "portland",
    "nashville", "dallas", "houston", "phoenix", "silicon valley", "bay area",
    "washington dc", "palo alto", "mountain view", "san diego", "charlotte",
    "raleigh", "minneapolis", "detroit", "pittsburgh", "boulder", "florida",
    "illinois", "colorado", "virginia", "georgia", "ohio", "michigan",
    "new jersey", "maryland", "connecticut", "massachusetts", "tennessee",
    "north carolina", "south carolina", "arizona", "nevada", "utah", "oregon",
    "washington state", "pennsylvania", "wisconsin", "minnesota", "missouri",
    "indiana", "kentucky", "louisiana", "alabama", "oklahoma", "kansas",
    "nebraska", "iowa", "arkansas", "mississippi", "idaho", "montana",
    "new mexico", "hawaii", "alaska", "delaware", "rhode island", "vermont",
    "new hampshire", "maine", "wyoming", "west virginia",
)

CPA_HEADLINE_PHRASES = (
    "cpa", "certified public accountant", "managing partner", "name partner",
    "founding partner", "senior partner", "tax partner", "audit partner",
    "partner |", "| partner", "partner, cpa", "cpa firm", "public accounting",
)

CPA_TEXT_PHRASES = (
    "cpa firm", "accounting firm", "public accounting", "certified public accountant",
    "managing partner", "name partner", "founding partner", "tax partner",
    "audit partner", "assurance partner", "advisory partner", "partner in the firm",
)

CPA_NEGATIVE = (
    "venture capital firm", "vc firm", "venture fund", "private equity firm",
    "hedge fund", "mutual fund", "investment bank", "wealth management",
    "recruiter", "talent acquisition", "real estate agent", "real estate broker",
    "law firm", "attorney at law", "legal counsel", "insurance agency",
    "marketing agency", "staffing agency", "angel investor",
)

COMPANY_PAGE_RE = [
    re.compile(r"\bis a\b.{0,100}\bcompany\b", re.I),
    re.compile(r"\bemploys \d+ people\b", re.I),
    re.compile(r"Venture Capital and Private Equity Principals", re.I),
    re.compile(r"\bhigher education institution\b", re.I),
    re.compile(r"\bgovernment agency\b", re.I),
    re.compile(r"\bnonprofit organization\b", re.I),
    re.compile(r"\bFinancial Services company\b", re.I),
]

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
PHONE_RE = re.compile(r"(?:\+1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}\b")
LINKEDIN_RE = re.compile(
    r"https?://(?:www\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+/?",
    re.I,
)
LINKEDIN_SLUG_RE = re.compile(r"linkedin\.com/in/([A-Za-z0-9\-_%]+)", re.I)
US_STATE_CITY_RE = re.compile(
    r"([A-Za-z .'\-]+,\s*[A-Za-z .'\-]+,\s*United States)"
    r"|([A-Za-z .'\-]+,\s*[A-Z]{2})\s*\(US\)"
    r"|Greater\s+[A-Za-z .'\-]+\s+Area\s*\(US\)"
    r"|([A-Za-z .'\-]+ Bay Area)\s*\(US\)",
)


def normalize_linkedin(url: str) -> str:
    if not url:
        return ""
    candidate = url.strip()
    if "/posts/" in candidate or "/company/" in candidate or "/school/" in candidate:
        return ""
    if not candidate.startswith("http"):
        candidate = "https://" + candidate.lstrip("/")
    m = LINKEDIN_RE.search(candidate)
    if not m:
        return ""
    u = m.group(0).split("?")[0].rstrip("/")
    low = u.lower()
    if any(sub in low for sub in NON_US_LINKEDIN_SUBDOMAINS):
        return ""
    return u


def linkedin_slug(url: str) -> str:
    m = LINKEDIN_SLUG_RE.search(url or "")
    return m.group(1).lower().rstrip("/") if m else ""


def is_valid_person_name(name: str) -> bool:
    if not name or len(name) < 2 or len(name) > 80:
        return False
    low = name.lower().strip()
    if low in {"cpa", "linkedin", "profile", "partner", "managing partner", "accountant"}:
        return False
    if re.search(r"https?://|linkedin\.com|###|^\d|\.com\b|\.org\b", name, re.I):
        return False
    if re.search(r"\bis a\b", name, re.I):
        return False
    if sum(c.isalpha() for c in name) < 2:
        return False
    return True


def is_company_or_org_page(title: str, text: str) -> bool:
    blob = f"{title} {text}"
    return any(p.search(blob) for p in COMPANY_PAGE_RE)


def has_cpa_signal(title: str, text: str) -> bool:
    headline = (title or "").lower()
    if any(p in headline for p in CPA_HEADLINE_PHRASES):
        return True
    snippet = (text or "")[:900].lower()
    return any(p in snippet for p in CPA_TEXT_PHRASES)


def is_non_us_text(text: str) -> bool:
    low = (text or "").lower()
    return any(m in low for m in NON_US_TEXT_MARKERS)


def is_us_location(text: str) -> bool:
    t = (text or "").lower()
    if is_non_us_text(t):
        return False
    if "united states" in t or "(us)" in t or ", us" in t:
        return True
    return any(h in t for h in US_LOCATION_HINTS)


def extract_location(text: str, title: str = "") -> str:
    blob = f"{title} {text}"
    if is_non_us_text(blob):
        return ""
    m = US_STATE_CITY_RE.search(blob)
    if m:
        for g in m.groups():
            if g:
                return g
    m2 = re.search(
        r"([A-Za-z .'\-]+,\s*(?:AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|"
        r"LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|"
        r"SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC))\b",
        blob,
    )
    if m2:
        return m2.group(1)
    if is_us_location(blob):
        for hint in US_LOCATION_HINTS:
            if hint in blob.lower() and hint not in (
                "united states", "usa", "u.s.", "(us)", ", us",
            ):
                return hint.title()
    return ""


def is_strict_cpa_partner(title: str, summary: str, firm_type: str = "") -> bool:
    """Deprecated. Kept only for backward compatibility with any external
    caller that still imports it directly.

    The live qualification path (parse_search_hit, parse_exa_result,
    passes_quality_bar) no longer calls this — it calls
    matches_target_criteria() against the active TargetConfig instead, so
    every campaign (CPA or otherwise) goes through the same generic code
    path. This function is equivalent to
    matches_target_criteria(row, CPA_PARTNER_PRESET) for the title/industry
    dimension, plus the CPA negative-phrase guard, preserved verbatim so old
    behaviour and any existing external callers don't change.
    """
    if firm_type and "cpa" in firm_type.lower():
        return True
    if not has_cpa_signal(title, summary):
        return False
    if is_company_or_org_page(title, summary):
        return False
    blob = f"{title} {summary}".lower()
    if any(n in blob for n in CPA_NEGATIVE):
        if not any(
            p in blob for p in ("cpa", "certified public accountant", "accounting firm", "public accounting")
        ):
            return False
    return True


# ---------------------------------------------------------------------------
# Generic qualification layer (Day 3): matches_target_criteria()
#
# This is the one qualification function every campaign goes through now,
# CPA or not. It is driven entirely by a TargetConfig instance — there is no
# CPA-specific code path left in the live discovery flow. The CPA preset
# (target_config.CPA_PARTNER_PRESET) just happens to be a TargetConfig like
# any other; it gets no special treatment here.
# ---------------------------------------------------------------------------

# Confidence labels allowed on InvestorRow["age_confidence"]. "none" and ""
# both mean "don't treat this as usable" — matches_target_criteria will skip
# age filtering for a row rather than reject it, since we never invent ages.
AGE_CONFIDENCE_LEVELS = ("high", "medium", "low", "none")


def _blob(row: InvestorRow) -> str:
    """Lowercased text blob — legacy, kept only for is_strict_cpa_partner()'s
    backward-compat code path. matches_target_criteria()/qualify_row() no
    longer use this; see build_evidence_text() below for the current,
    contamination-aware, evidence-gated replacement.
    """
    parts = [
        str(row.get("profile_title", "") or ""),
        str(row.get("summary", "") or ""),
        str(row.get("industries", "") or ""),
        str(row.get("company_name", "") or ""),
    ]
    return " ".join(parts).lower()


def _any_term_in(blob: str, terms: list[str]) -> bool:
    return any(t.strip().lower() in blob for t in terms if t and t.strip())


# ---------------------------------------------------------------------------
# Generic, configurable keyword/industry evidence system.
#
# The problem this replaces: matching a configured term as a loose substring
# anywhere in a row's raw text made `keywords`/`industries` behave like
# search terms rather than qualification criteria — a company in an
# unrelated field would "qualify" just because the configured word happened
# to appear somewhere in a noisy search snippet (possibly about a different
# person entirely, glued into the same DDGS hit).
#
# Nothing below is specific to any industry or term — "AI", "SaaS",
# "fintech", "blockchain" are never hard-coded. The system is driven purely
# by whatever terms a TargetConfig configures, and works identically for all
# of them:
#
#   1. A term counts as strong evidence when it appears in the row's
#      profile_title or company_name — curated, single-person fields, not
#      raw multi-hit search text — OR when it appears near a generic,
#      industry-agnostic descriptor noun ("platform", "company", "software",
#      "protocol", "service", ...) anywhere in the row's (decontaminated)
#      text. That proximity requirement is what stops a bare, out-of-context
#      mention of the term from counting as evidence.
#   2. Before any of that runs, `summary` is dropped from the evidence text
#      entirely if it looks like it may have glued together multiple
#      people's search results (a real, observed failure mode of this
#      pipeline's DDGS-derived data) — see build_evidence_text() /
#      _looks_multi_person(). This prevents attributing one person's
#      company/keywords to a different profile.
# ---------------------------------------------------------------------------

# Generic, industry-agnostic nouns that, when found close to a configured
# keyword/industry term, indicate the term is describing an actual
# business/product/role rather than floating in unrelated text. This list is
# deliberately generic — it works the same way whether the configured term
# is "AI", "fintech", "blockchain", or anything else a campaign configures.
_EVIDENCE_DESCRIPTOR_NOUNS = frozenset({
    "company", "companies", "platform", "platforms", "software", "product",
    "products", "solution", "solutions", "startup", "startups", "firm",
    "firms", "business", "businesses", "app", "apps", "application",
    "applications", "service", "services", "technology", "technologies",
    "tool", "tools", "protocol", "protocols", "network", "exchange",
    "provider", "system", "systems", "marketplace", "infrastructure",
    "engine", "suite", "venture", "ventures",
})

# Words of context searched on either side of a term match for a descriptor
# noun. Deliberately tight (a handful of words, not a whole sentence) — this
# is what distinguishes "AI-powered automation platform" (the descriptor is
# immediately adjacent) from a chemical company whose site merely mentions
# "AI temperature sensor" in one sentence and happens to call itself a
# "firm" several words earlier in the same snippet. A wide window would
# treat both as equally strong evidence; this doesn't.
_WORD_PROXIMITY = 3

_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def _term_regex(term: str) -> "re.Pattern[str]":
    escaped = re.escape((term or "").strip())
    return re.compile(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", re.I)


# Matches a "Full Name - " prefix the way DDGS/LinkedIn search-result titles
# format a person's own headline, e.g. "Austin Maxwell - Cofounder - Kanga
# Coolers". Used to count how many distinct people's name+title headers
# appear to be glued into one title string.
_NAME_TITLE_PREFIX_RE = re.compile(
    r"(?:^|\|)\s*([A-Z][a-zA-Z.'\-]+(?:\s+[A-Z][a-zA-Z.'\-]+){1,3})\s+-\s+"
)

# Matches DDGS/Bing's "View <Name>'s profile on LinkedIn" boilerplate, which
# names whichever profile that sentence is actually describing. Two or more
# distinct names here is a hard signal that more than one person's search
# hit got glued together in the same field.
_VIEW_PROFILE_RE = re.compile(
    r"View\s+([A-Z][a-zA-Z.'\-]+(?:\s+[A-Z][a-zA-Z.'\-]+){1,3})(?:'s|\u2019s)\s+profile\s+on\s+LinkedIn",
    re.I,
)


def _distinct_name_count(pattern: "re.Pattern[str]", text: str) -> int:
    names = {m.group(1).strip().lower() for m in pattern.finditer(text or "")}
    return len(names)


def _looks_multi_person(text: str) -> bool:
    """Heuristic for DDGS snippets that glued multiple people's search
    results into one field, e.g. "... - LinkedInJohn Doe - CTO at ... -
    LinkedInJane Roe ..." or two separate "Experience:" blocks, or two
    "Name - Title" headline prefixes, or two different "View X's profile on
    LinkedIn" sentences. When detected, the field is unsafe to use as
    evidence for *this* row — neither as free text nor as a "curated"
    direct-match field — and is dropped entirely, rather than risk
    attributing someone else's company/keywords to the wrong profile.
    """
    if not text:
        return False
    if text.count("LinkedIn") >= 2:
        return True
    if text.count("Experience:") >= 2:
        return True
    if _distinct_name_count(_NAME_TITLE_PREFIX_RE, text) >= 2:
        return True
    if _distinct_name_count(_VIEW_PROFILE_RE, text) >= 2:
        return True
    return False


def is_contaminated_hit(title: str, body: str) -> bool:
    """Whole-result contamination check, used to REJECT a raw search hit
    outright (before a candidate is ever built) rather than build a
    candidate from it and merely blank the tainted fields downstream.

    This is the task-1 "reject instead of extract" gate: when a title or
    snippet clearly glues more than one LinkedIn profile together (multiple
    "Name - Title" headers, multiple "View X's profile on LinkedIn"
    sentences, repeated "LinkedIn"/"Experience:" markers), there is no safe
    way to know which sentences belong to the person whose /in/ URL we
    actually have — so the whole hit is rejected rather than risk
    attributing another person's name, title, company, or age evidence to
    the target.

    See decontaminate_hit_text() for the salvage path used upstream (in
    sources.ddgs_search.parse_search_hit) BEFORE this check runs: most
    contamination is a second person's hit glued onto the *end* of the
    first person's (the target's) own clean data, so truncating right at
    the boundary and re-checking here on the truncated text recovers a
    real candidate instead of discarding one purely because of what came
    after their own data ended.
    """
    return _looks_multi_person(title or "") or _looks_multi_person(body or "")


def _first_contamination_boundary(text: str) -> int:
    """The earliest character index in `text` where a second, unrelated
    person's LinkedIn hit appears to start gluing onto the first — or -1 if
    no such boundary is detected. Deliberately the EARLIEST boundary across
    every signal checked (not the first signal that happens to fire), so
    truncating text[:boundary] is guaranteed to drop all of the second
    person's data, never include part of it.

    Only reacts to signals that are reliably *positional* (a second
    "LinkedIn" marker; a second distinct "Name - Title" prefix; a second
    distinct "View X's profile on LinkedIn" sentence) — deliberately does
    NOT attempt to find a boundary for the "two 'Experience:' blocks"
    signal, since that one page-count signal has no reliable single split
    point (is_contaminated_hit still rejects on that signal, unchanged).
    """
    if not text:
        return -1
    boundaries: list[int] = []

    # 2+ "LinkedIn" mentions is the same over-concatenation signal
    # is_contaminated_hit's own count check uses -- once that's true, the
    # first occurrence is where person 1's own data ends and the glued-on
    # boilerplate for person 2 begins (e.g. "...Partner at Acme - LinkedIn
    # Jane Roe - CTO..."), so that's the truncation point, not the second
    # one (which would still leave a second person's data attached).
    li_idxs = [m.start() for m in re.finditer("LinkedIn", text)]
    if len(li_idxs) >= 2:
        boundaries.append(li_idxs[0])

    for pattern in (_NAME_TITLE_PREFIX_RE, _VIEW_PROFILE_RE):
        seen_names: set[str] = set()
        for m in pattern.finditer(text):
            key = m.group(1).strip().lower()
            if key in seen_names:
                continue
            seen_names.add(key)
            if len(seen_names) == 2:
                boundaries.append(m.start())
                break

    if not boundaries:
        return -1
    return min(boundaries)


def decontaminate_hit_text(text: str) -> str:
    """Truncate `text` to just its first person's segment when it looks
    like a second, unrelated LinkedIn hit was glued onto the end of it
    (see _first_contamination_boundary) — recovering the target's own,
    genuinely clean data instead of discarding the whole hit outright.

    Returns `text` completely unchanged when no contamination boundary is
    found (the overwhelming majority of hits — this is a no-op there).
    Never used to disentangle interleaved/mixed text, only to drop a
    trailing second segment wholesale, so it can never attribute any part
    of a second person's data to the target.
    """
    boundary = _first_contamination_boundary(text)
    if boundary <= 0:
        return text
    return text[:boundary].strip()


def _decontaminated_fields(row: InvestorRow) -> tuple[str, str, str, str]:
    """Returns (title, company_name, industries, combined_text) with any
    field that looks like it may contain more than one person's
    search-result data blanked out entirely — including from the "curated
    field" direct-match fast path, not just the free-text proximity search.
    A contaminated title (e.g. two people's LinkedIn hits glued together)
    must never be trusted just because it's normally a curated field.
    """
    title = str(row.get("profile_title", "") or "")
    company = str(row.get("company_name", "") or "")
    industries_field = str(row.get("industries", "") or "")
    summary = str(row.get("summary", "") or "")

    if _looks_multi_person(title):
        title = ""
    if _looks_multi_person(summary):
        summary = ""

    parts = [title, company, industries_field, summary]
    text = " ".join(p for p in parts if p)
    return title, company, industries_field, text


def build_evidence_text(row: InvestorRow) -> str:
    """Decontaminated, person-scoped text used for keyword/industry/
    exclude-keyword proximity evidence checks. See _decontaminated_fields().
    """
    _, _, _, text = _decontaminated_fields(row)
    return text


def term_evidence(text: str, term: str, row: InvestorRow) -> tuple[bool, str]:
    """Does `term` have strong, person-scoped evidence?

    Returns (matched, evidence_snippet). Two ways to match, both computed
    from the same decontaminated fields as `text` (see
    _decontaminated_fields) so a contaminated field can never slip in as
    evidence via either path:

      1. The term appears verbatim in the row's (decontaminated)
         profile_title, company_name, or industries field — these are
         short, curated/structured values (a headline, a resolved company
         name, a classified industry tag), not raw scraped sentences, so a
         term appearing in one of them counts as strong evidence on its
         own.
      2. The term appears in the row's (decontaminated) combined text
         within _WORD_PROXIMITY words of a generic descriptor noun (see
         _EVIDENCE_DESCRIPTOR_NOUNS) — e.g. "AI software", "blockchain
         protocol". This tight word-distance requirement (not a whole
         sentence/snippet) is what stops an out-of-context, incidental
         mention of the term from counting — e.g. a chemical company that
         merely mentions an "AI temperature sensor" in one sentence and
         calls itself a "firm" several words away does NOT count.

    `text` is accepted as a parameter (rather than always recomputed here)
    so callers can reuse one build_evidence_text(row) call across multiple
    terms/dimensions without recomputing it each time; it must always be
    build_evidence_text(row) for the same row passed in `row`.
    """
    term = (term or "").strip()
    if not term:
        return False, ""
    pattern = _term_regex(term)

    title, company, industries_field, _ = _decontaminated_fields(row)
    if pattern.search(title):
        return True, title.strip()[:200]
    if pattern.search(company):
        return True, company.strip()[:200]
    if pattern.search(industries_field):
        return True, industries_field.strip()[:200]

    tokens = [(m.group(0), m.start(), m.end()) for m in _WORD_RE.finditer(text or "")]
    lower_tokens = [t[0].lower() for t in tokens]
    term_tokens = term.lower().split()
    n = len(term_tokens)
    if n == 0 or not tokens:
        return False, ""
    for i in range(len(lower_tokens) - n + 1):
        if lower_tokens[i : i + n] != term_tokens:
            continue
        lo = max(0, i - _WORD_PROXIMITY)
        hi = min(len(tokens), i + n + _WORD_PROXIMITY)
        window_words = lower_tokens[lo:hi]
        if any(w in _EVIDENCE_DESCRIPTOR_NOUNS for w in window_words):
            snippet = text[tokens[lo][1] : tokens[hi - 1][2]]
            return True, snippet.strip()[:200]
    return False, ""


def dimension_evidence(
    text: str, terms: list[str], row: InvestorRow
) -> tuple[bool, str, str]:
    """OR across `terms` — any single matching term is enough (per the
    configured-dimension semantics: multiple values in one dimension are
    alternatives). Returns (matched, matched_term, evidence_snippet).
    """
    for term in terms or []:
        matched, snippet = term_evidence(text, term, row)
        if matched:
            return True, term, snippet
    return False, "", ""


def exclude_evidence(text: str, terms: list[str], row: InvestorRow) -> tuple[bool, str]:
    """Does any exclude_keywords term have person-scoped evidence?

    Deliberately simpler than dimension_evidence(): exclusion is a safety
    net, so any occurrence of the term in the row's own (decontaminated,
    person-scoped) evidence text is enough — no descriptor-noun proximity
    is required. It still only ever looks at build_evidence_text()'s
    decontaminated text (title/company_name/industries, plus summary only
    when not flagged as possibly multi-person), never raw, unscoped search
    text, so a term merely appearing somewhere in unrelated snippet content
    is not treated as being "associated with" this person.
    """
    for term in terms or []:
        t = (term or "").strip()
        if not t:
            continue
        if _term_regex(t).search(text or ""):
            return True, t
    return False, ""


def _titles_ok(row: InvestorRow, titles: list[str]) -> bool:
    """Job title matching is scoped to profile_title only (the curated
    headline/title field), not the full evidence text — a title dimension
    should reflect what the person's own headline says, not something
    mentioned incidentally in a snippet.

    Uses the decontaminated title (see _decontaminated_fields) rather than
    the raw field directly: a title glued from multiple people's search
    hits must never be trusted as "this person's own headline" just because
    profile_title is normally a curated field.
    """
    if not titles:
        return True
    title, _, _, _ = _decontaminated_fields(row)
    low = title.lower()
    return any(t.strip().lower() in low for t in titles if t and t.strip())


def _location_ok(row: InvestorRow, target_locations: list[str]) -> bool:
    """True if no location constraint, or the row matches at least one of
    the target's location strings.

    "United States" (and common spellings) reuses the existing US location
    heuristics (state/city name lists, non-US markers) for broad recall.
    Any other location string is matched as a case-insensitive substring
    against the row's location field plus its (decontaminated) evidence
    text, so a campaign targeting e.g. "Austin" or "London" works without
    new US-only code.

    For "United States" specifically, is_us_location() only recognizes a
    necessarily-incomplete hard-coded list of major cities/state names —
    a real US city that isn't on that list (e.g. "Encinitas") would
    otherwise be wrongly rejected even though nothing marks it as non-US.
    So when the positive city/state check misses, but the row does have
    *some* stated location and nothing in it is flagged non-US (see
    NON_US_TEXT_MARKERS / NON_US_LINKEDIN_SUBDOMAINS — already checked
    once at discovery time in extract_location() and again in
    qualify_row()'s own is_non_us_text() check before this function is
    ever reached), treat it as a plausible US match rather than reject a
    real prospect purely for living somewhere not on the shortlist.
    """
    if not target_locations:
        return True
    row_loc = str(row.get("location", "") or "")
    blob = f"{row_loc} {build_evidence_text(row)}"
    low = blob.lower()
    for loc in target_locations:
        norm = loc.strip().lower()
        if norm in ("united states", "usa", "u.s.", "us"):
            if is_us_location(blob):
                return True
            if row_loc.strip() and not is_non_us_text(blob):
                return True
        elif norm and norm in low:
            return True
    return False


def _parse_int(value: str) -> int | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    m = re.search(r"-?\d+", s)
    if not m:
        return None
    try:
        return int(m.group(0))
    except ValueError:
        return None


def _company_size_ok(row: InvestorRow, target: TargetConfig) -> bool:
    """True if no company-size bounds are configured, or the row has no
    company-size data (nothing to filter on), or the available figure falls
    inside the configured [min, max] range.

    We only ever filter on company size when the row actually has it — a
    missing value is "unknown", not "excluded".
    """
    if target.company_size_min is None and target.company_size_max is None:
        return True
    raw = row.get("company_size", "")
    size = _parse_int(raw) if raw else None
    if size is None:
        return True  # data not available -> don't reject on it
    if target.company_size_min is not None and size < target.company_size_min:
        return False
    if target.company_size_max is not None and size > target.company_size_max:
        return False
    return True


def _age_ok(row: InvestorRow, target: TargetConfig) -> bool:
    """True if no age bounds are configured, or the row has no usable age
    proxy, or the stored (explicitly labelled) proxy falls inside range.

    Age is never invented. This only ever reads a value that some upstream
    step already stored in row["age"] alongside a non-"none" age_confidence
    and a populated age_source — it does not infer anything itself. A row
    with no age, or age_confidence == "none"/"", is treated as "unknown"
    and is NOT rejected, exactly like missing company size.
    """
    if target.age_min is None and target.age_max is None:
        return True
    confidence = str(row.get("age_confidence", "") or "").strip().lower()
    if confidence in ("", "none"):
        return True  # no usable proxy -> don't reject on it
    age = _parse_int(row.get("age", ""))
    if age is None:
        return True
    if target.age_min is not None and age < target.age_min:
        return False
    if target.age_max is not None and age > target.age_max:
        return False
    return True


def matches_target_criteria(row: InvestorRow, target: TargetConfig | None = None) -> bool:
    """Generic replacement for is_strict_cpa_partner(): does this profile
    match the given campaign's criteria?

    Every dimension is optional-and-AND'd: an empty list/None on the
    TargetConfig means "no constraint on this dimension" (never "match
    nothing"), but any dimension that IS configured must be satisfied for
    the row to qualify. Within a dimension, any one matching term is enough
    (e.g. titles=["Founder","CEO"] matches either word) — multiple values
    inside one dimension are alternatives (OR); different dimensions are
    requirements (AND).

    - titles: substring-matched against profile_title only (see
      _titles_ok). Matched against target.expanded_titles (the configured
      titles plus any configured title_synonyms) — see TargetConfig.
    - industries / keywords: matched via the generic evidence system above
      (dimension_evidence / term_evidence) against target.expanded_industries
      / target.expanded_keywords (base terms plus configured synonyms) — a
      term must appear in a curated field (title/company_name) or near a
      generic descriptor noun in decontaminated text, not just anywhere in
      a raw snippet. No term (AI, SaaS, fintech, or anything else) is
      special-cased; behavior is driven entirely by what's configured.
    - exclude_keywords: reject if any of these terms have person-scoped
      evidence (see exclude_evidence), regardless of anything else.
    - locations: see _location_ok().
    - company_size / age: only enforced when the row actually carries that
      data (see _company_size_ok / _age_ok) — never rejects for missing
      data, and age is never treated as fact, only as a labelled proxy.
    """
    target = target or get_active_target()
    evidence_text = build_evidence_text(row)

    if target.exclude_keywords:
        excluded, _ = exclude_evidence(evidence_text, target.exclude_keywords, row)
        if excluded:
            return False
    if target.titles and not _titles_ok(row, target.expanded_titles):
        return False
    if target.industries:
        matched, _, _ = dimension_evidence(evidence_text, target.expanded_industries, row)
        if not matched:
            return False
    if target.keywords:
        matched, _, _ = dimension_evidence(evidence_text, target.expanded_keywords, row)
        if not matched:
            return False
    if not _location_ok(row, target.locations):
        return False
    if not _company_size_ok(row, target):
        return False
    if not _age_ok(row, target):
        return False
    return True


# ---------------------------------------------------------------------------
# Explicit qualification result with a human-readable reason (Day 3+).
#
# matches_target_criteria() above stays a pure bool for backward
# compatibility (existing callers / tests). qualify_row() wraps it, adds a
# couple of hard, target-independent sanity checks (person page, resolvable
# LinkedIn URL, company isn't an unrelated institution), and — critically —
# explains *why* a row failed, so the orchestrator's "qualify" phase can
# write a `qualification_reason` back to the CSV instead of a bare pass/fail.
# ---------------------------------------------------------------------------

# Organizations that are essentially never the SaaS/tech "company" a
# founder-qualification campaign is looking for, even when they legitimately
# are the person's current employer (e.g. university staff, government
# employees). These are institutional-type markers, not brand names, so they
# are safe to hard-code rather than making every campaign configure them via
# exclude_keywords.
NON_COMPANY_ORG_MARKERS = (
    "university", "college", "institute", "polytechnic", "school",
    "community college", "academy", "seminary", "board of education",
    "department of education", "city of ", "county of ",
    "state of ", "government of", "u.s. department", "us department",
    "municipality", "nonprofit organization", "non-profit organization",
)


def is_non_company_org(name: str) -> bool:
    """True if `name` reads like a university/school/government body rather
    than a company (SaaS or otherwise). Used to keep alma maters and public
    institutions out of company_name-driven qualification, even when they
    are truthfully the person's employer.
    """
    low = f" {(name or '').lower()} "
    return any(m in low for m in NON_COMPANY_ORG_MARKERS)


# Bare industry/technology/business-type dictionary terms that are
# virtually never a genuine company *brand name* on their own -- a real
# company's name is essentially never identical to a plain category word
# like this. Deliberately generic/industry-agnostic (never named after one
# vertical) and conservative: only exact whole-token matches are treated
# as generic, so "Acme AI" or "SaasRise" are untouched -- only a name that
# reduces entirely to these words (e.g. "AI", "SaaS Company") is rejected.
GENERIC_COMPANY_TERMS = frozenset(
    {
        "ai", "ml", "iot", "vr", "ar", "nlp", "llm", "api", "erp", "crm",
        "saas", "paas", "iaas", "b2b", "b2c", "it", "hr", "pr", "seo",
        "tech", "technology", "technologies", "software", "solutions",
        "platform", "cloud", "data", "digital", "automation", "analytics",
        "startup", "company", "corporation", "corp", "inc", "llc",
    }
)


def is_domain_guessable_company_name(name: str) -> bool:
    """True only if `name` is specific enough to justify *guessing* a
    domain from it (e.g. via the vendored slugify_domain()).

    Deliberately conservative: a wrong domain guess doesn't just silently
    fail, it can produce a real, live, but wholly unrelated domain (e.g.
    "AI" -> ai.com) that then passes MX checking and looks like a found
    email when it's actually just noise. Rejects:
      - empty input
      - anything is_valid_company_name() already rejects
      - a university/school/government body (is_non_company_org)
      - a bare generic industry/business term (GENERIC_COMPANY_TERMS),
        exact match only
      - a name that, after stripping generic business-type words token by
        token, has nothing distinctive left at all (e.g. "SaaS Company")
    """
    name = (name or "").strip()
    if not name:
        return False
    if not is_valid_company_name(name):
        return False
    if is_non_company_org(name):
        return False
    if name.lower() in GENERIC_COMPANY_TERMS:
        return False
    tokens = [t for t in re.split(r"\s+", name) if t]
    meaningful = [t for t in tokens if t.lower() not in GENERIC_COMPANY_TERMS]
    return bool(meaningful)


# Cue words that signal "the institution named nearby is where this person
# studies/studied", not their employer -- e.g. "junior MIS student at RIT".
# Broader than _EDU_ENROLLMENT_CLAUSE_RE above: that regex only fires when
# the institution's own name literally contains a word like "University"/
# "Institute"; this one also catches the common case where the institution
# is referred to only by an ambiguous short-form/abbreviation (e.g. "RIT")
# that reads exactly like a plausible company name on its own.
_ENROLLMENT_CUE_RE = re.compile(
    r"\b(?:student|studying|studies|attending|enrolled|alumnus|alumna|alumni)\b",
    re.I,
)
_ENROLLMENT_WINDOW_CHARS = 60


def _is_enrollment_mention(name: str, evidence_text: str) -> bool:
    """True if `name` only appears in `evidence_text` right after an
    enrollment cue word (e.g. "student at RIT") -- i.e. it reads like the
    person's school, not their employer, even when the institution's own
    name gives no other textual hint of that (see comment above).
    """
    name = (name or "").strip()
    if not name or not evidence_text:
        return False
    low_text = evidence_text.lower()
    low_name = name.lower()
    name_re = re.compile(r"\b" + re.escape(low_name) + r"\b")
    for m in _ENROLLMENT_CUE_RE.finditer(low_text):
        window = low_text[m.end(): m.end() + _ENROLLMENT_WINDOW_CHARS]
        if name_re.search(window):
            return True
    return False


# Domains that are never themselves a company's own site -- social
# networks, mail providers, aggregators -- so a mention of one in free
# text is never treated as company-domain evidence.
_NON_COMPANY_DOMAIN_MARKERS = frozenset(
    {
        "linkedin.com", "gmail.com", "yahoo.com", "outlook.com", "hotmail.com",
        "icloud.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
        "medium.com", "github.com", "google.com", "youtube.com", "example.com",
    }
)

_URL_DOMAIN_RE = re.compile(
    r"\b(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9\-]*\.(?:com|io|ai|co|net|org|dev|app))\b",
    re.I,
)


def extract_domain_from_text(text: str) -> str:
    """The first plausible company domain literally mentioned in free text
    (e.g. "I'm building SaasRise (www.saasrise.com)") -- the strongest
    possible domain evidence available, since it isn't a guess at all, just
    a read. Skips known non-company domains (social networks, mail
    providers, ...). Returns "" if nothing usable is found -- never guesses.
    """
    for m in _URL_DOMAIN_RE.finditer(text or ""):
        domain = m.group(1).lower()
        if domain in _NON_COMPANY_DOMAIN_MARKERS:
            continue
        return domain
    return ""


def qualify_row(row: InvestorRow, target: TargetConfig | None = None) -> tuple[bool, str]:
    """Full qualification check for one prospect, with an explanation.

    Returns (True, "") if the row genuinely qualifies. Returns (False,
    reason) otherwise, where `reason` names the first failing check(s) in
    plain language, e.g. "no strong evidence of required industry (SaaS)".
    This is what the orchestrator's "qualify" phase stores in
    row['qualification_reason'].

    As a side effect, always writes row['keyword_relevance'],
    row['keyword_evidence'], and row['industry_evidence'] — generic,
    industry-agnostic audit fields (not "ai_relevance" or similar) so any
    campaign, regardless of what industries/keywords it configures, gets
    the same transparency into *why* it matched or didn't.

    Order of checks: cheap, target-independent sanity checks first (valid
    person name, not a company/org page, resolvable LinkedIn URL, company
    isn't a university/government body), then the full
    matches_target_criteria() dimensions, each reported individually so a
    disqualified row's reason is actually useful instead of a bare "no".
    """
    target = target or get_active_target()
    row["keyword_relevance"] = ""
    row["keyword_evidence"] = ""
    row["industry_evidence"] = ""

    name = row.get("name", "")
    if not is_valid_person_name(name):
        return False, "missing or invalid person name"

    title = row.get("profile_title", "")
    summary = row.get("summary", "")
    if is_company_or_org_page(title, summary):
        return False, "profile is a company/organization page, not a person"

    li = normalize_linkedin(row.get("linkedin_url", ""))
    if not li:
        return False, "no resolvable US LinkedIn /in/ profile URL"

    company = str(row.get("company_name", "") or "")
    if company and is_non_company_org(company):
        return False, f"company ('{company}') is a university/school/government body, not a target company"

    if is_non_us_text(f"{title} {summary} {row.get('location', '')}"):
        return False, "profile evidence points to a non-US location"

    evidence_text = build_evidence_text(row)
    reasons: list[str] = []

    if target.exclude_keywords:
        excluded, term = exclude_evidence(evidence_text, target.exclude_keywords, row)
        if excluded:
            reasons.append(f"excluded keyword '{term}' is associated with this profile")

    if target.titles and not _titles_ok(row, target.titles):
        reasons.append("job title does not match any configured target title")

    if target.industries:
        matched, _, snippet = dimension_evidence(evidence_text, target.industries, row)
        row["industry_evidence"] = snippet
        if not matched:
            reasons.append(
                f"no strong evidence of required industry ({', '.join(target.industries)}) "
                "tied to this person's role/company"
            )

    if target.keywords:
        matched, _, snippet = dimension_evidence(evidence_text, target.keywords, row)
        row["keyword_evidence"] = snippet
        row["keyword_relevance"] = "strong" if matched else "none"
        if not matched:
            reasons.append(
                f"no strong evidence of required keyword ({', '.join(target.keywords)}) "
                "tied to this person's role/company"
            )

    if not _location_ok(row, target.locations):
        reasons.append("not evidenced as located in a target location")
    if not _company_size_ok(row, target):
        reasons.append("company size outside configured range")
    if not _age_ok(row, target):
        reasons.append("age proxy outside configured range")

    if reasons:
        return False, "; ".join(reasons)
    return True, ""


# --- Age proxy extraction (evidence-only, never invented) -------------------
#
# Two deterministic, regex-based signals, checked in priority order:
#   1. An explicitly stated age ("Age: 34", "34 years old") -> high
#      confidence, since it's a direct statement, not an inference.
#   2. A stated graduation year ("Class of 2014", "graduated in 2014") ->
#      medium confidence proxy, assuming a typical ~22-year-old undergraduate
#      graduation age. This is exactly the "graduation year" proxy the spec
#      calls out, and it is always labelled as a proxy in age_source.
# No LLM guess, appearance, or name-based inference is ever used. If neither
# pattern is found, callers get ("", "", "none") and must leave age blank.

_EXPLICIT_AGE_RE = re.compile(r"\bage[d]?\s*[:\-]?\s*(\d{2})\b", re.I)
_AGE_YEARS_OLD_RE = re.compile(r"\b(\d{2})[\s-]*(?:years?[\s-]old|y\.?o\.?)\b", re.I)
_GRAD_YEAR_RE = re.compile(
    r"\b(?:class of|graduated(?:\s+in)?|graduating\s+class\s+of)\s*[:\-]?\s*(\d{4})\b",
    re.I,
)

# Typical age at undergraduate graduation, used only to derive a clearly
# labelled proxy — never presented as fact.
_TYPICAL_GRAD_AGE = 22
_MIN_PLAUSIBLE_AGE = 16
_MAX_PLAUSIBLE_AGE = 100


def extract_age_proxy(row: InvestorRow) -> tuple[str, str, str]:
    """Return (age, age_source, age_confidence) from explicit textual
    evidence only. Never infers from a name, appearance, or any signal other
    than text the profile/snippet actually states. Returns ("", "", "none")
    when there's nothing usable — callers must leave age blank in that case,
    not guess.
    """
    text = f"{row.get('profile_title', '')} {row.get('summary', '')}"
    if not text.strip():
        return "", "", "none"

    m = _EXPLICIT_AGE_RE.search(text) or _AGE_YEARS_OLD_RE.search(text)
    if m:
        age = int(m.group(1))
        if _MIN_PLAUSIBLE_AGE <= age <= _MAX_PLAUSIBLE_AGE:
            return (
                str(age),
                f"explicit age stated in profile text (matched {m.group(0).strip()!r})",
                "high",
            )

    m2 = _GRAD_YEAR_RE.search(text)
    if m2:
        year = int(m2.group(1))
        current_year = datetime.now(timezone.utc).year
        if 1950 <= year <= current_year:
            proxy_age = current_year - year + _TYPICAL_GRAD_AGE
            if _MIN_PLAUSIBLE_AGE <= proxy_age <= _MAX_PLAUSIBLE_AGE:
                return (
                    str(proxy_age),
                    (
                        f"proxy estimated from stated graduation year {year} "
                        f"(assumes ~{_TYPICAL_GRAD_AGE} years old at graduation) — not a stated age"
                    ),
                    "medium",
                )

    return "", "", "none"


# --- Age-proxy web-search enrichment ----------------------------------------
# extract_age_proxy() (above) and the LLM fallback (llm.extract_age_llm) both
# only ever look at text the discovery phase already scraped — the DDGS
# LinkedIn search snippet (profile_title + summary). In practice that text
# essentially never states an age or graduation year (it's a short headline/
# preview, not the person's full profile), so both passes are structurally
# unable to find evidence that was never fetched in the first place — they
# can only re-read the same empty text more carefully. The functions below
# add an actual new source of evidence: a couple of narrowly-targeted web
# searches per candidate, specifically for graduation-year phrasing. This is
# still governed by the exact same conservative rule as everything else
# here: the returned text is run back through extract_age_proxy() itself,
# so a search merely returning *results* is never treated as evidence by
# itself — only an explicit age or graduation year actually present in the
# returned text counts.


def build_age_search_queries(name: str) -> list[str]:
    """Two narrow, high-precision searches for graduation-year evidence.
    Deliberately not a broad "<name> age" search — that mostly returns
    unrelated people-search/background-check sites with no verifiable
    evidence, exactly the kind of ungrounded content this project's age
    policy is designed to avoid relying on.
    """
    name = (name or "").strip()
    if not name:
        return []
    return [
        f'"{name}" "class of" LinkedIn',
        f'"{name}" graduated university OR college',
    ]


def find_age_evidence_via_search(
    row: InvestorRow,
    search_fn: Callable[[str, int], list[dict[str, str]]],
    max_results: int = 5,
) -> tuple[str, str, str]:
    """Best-effort age-proxy enrichment via a couple of targeted web
    searches, for rows the (evidence-only) regex/LLM passes couldn't
    resolve from the discovery snippet alone.

    `search_fn(query, max_results) -> list[{"title","body",...}]` is
    injected rather than imported directly, so this is fully unit-testable
    offline with a fake search function (see
    test_discovery_contamination_day11.py) and so callers can point it at
    whichever search backend/config they're already using (this project
    uses sources.ddgs_search.ddgs_search(..., linkedin_only=False)).

    Returns ("", "", "none") if no query returns text containing genuine
    age/graduation-year evidence — never invents an age, and a query
    returning zero or irrelevant results is treated exactly like finding
    nothing, not like a signal in itself.
    """
    name = row.get("name", "")
    if not name:
        return "", "", "none"
    for query in build_age_search_queries(name):
        try:
            hits = search_fn(query, max_results)
        except Exception:
            continue
        for hit in hits or []:
            combined_text = f"{hit.get('title', '')} {hit.get('body', '')}"
            probe_row: InvestorRow = {"profile_title": combined_text, "summary": ""}  # type: ignore[typeddict-item]
            age, source, confidence = extract_age_proxy(probe_row)
            if confidence != "none":
                return age, f"web search enrichment ({query[:40]}...): {source}", confidence
    return "", "", "none"


# --- Company-name extraction validation/normalization -----------------------

LEGAL_SUFFIX_RE = re.compile(
    r",?\s*(?<!&\s)\b("
    r"L\.?L\.?C\.?|L\.?L\.?P\.?|Inc\.?|Incorporated|Ltd\.?|Limited|"
    r"PLC|P\.?L\.?C\.?|GmbH|Pvt\.?\s*Ltd\.?|Corp\.?|Corporation|"
    r"P\.?A\.?|P\.?C\.?|Co\.?"
    r")\.?\s*$",
    re.I,
)

_TITLE_ABBREVIATIONS = frozenset(
    {
        "ceo", "cto", "coo", "cfo", "cmo", "cgo", "cpo", "cro", "ciso",
        "vp", "svp", "evp", "hr", "pr", "pm",
    }
)

_COMPANY_INVALID_MARKERS = (
    "http://", "https://", "www.", "{", "}", "```", "n/a", "none", "unknown",
    "cannot be determined", "not provided", "not specified", "unable to",
    "i cannot", "i'm sorry", "as an ai", "the company", "employer",
    "as stated", "as mentioned", "based on", "according to", "it appears",
    "seems to be", "likely", "probably", "note that", "please", "sorry",
)

# Common sentence-connector words that virtually never appear as whole words
# inside a canonical company name; their presence signals an explanatory
# sentence rather than a bare name.
_COMPANY_SENTENCE_WORD_RE = re.compile(
    r"\b(is|are|was|were|this|that|therefore|however|because|since|so|"
    r"determine|determined|infer|inferred|context|profile|person)\b",
    re.I,
)

_COMPANY_LIST_SEPARATORS_RE = re.compile(r"[\n;]|,\s*[A-Z]|\bor\b|\band\b")


def clean_company_name(raw: str) -> str:
    """Trim whitespace/quotes and strip legal suffixes while keeping the brand name."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:\w+)?|```$", "", text).strip()
    text = text.strip("\"'` \t")
    text = re.sub(r"\s+", " ", text).strip()
    prev = None
    while prev != text and text:
        prev = text
        text = LEGAL_SUFFIX_RE.sub("", text).strip()
        text = text.rstrip(",.").strip()
    return text


def is_valid_company_name(name: str) -> bool:
    """Reject anything that isn't a single, plain-text canonical company name."""
    if not name:
        return False
    if len(name) > 100:
        return False
    low = name.lower()
    if any(marker in low for marker in _COMPANY_INVALID_MARKERS):
        return False
    # A bare job-title abbreviation (e.g. from "<Title> | <SecondTitle>"
    # headlines like "CEO | COO | Customer Success Executive", where a
    # separator-based extractor could otherwise misread the second title
    # as the company) is never itself a company name.
    if low.strip() in _TITLE_ABBREVIATIONS:
        return False
    if re.search(r"[{}\[\]<>]", name):
        return False
    if re.search(r"https?://|linkedin\.com|\.com\b|\.org\b|\.net\b", name, re.I):
        return False
    if len(name.split()) > 8:
        return False
    if _COMPANY_SENTENCE_WORD_RE.search(name):
        return False
    if _COMPANY_LIST_SEPARATORS_RE.search(name) and "&" not in name:
        return False
    if sum(c.isalpha() for c in name) < 2:
        return False
    return True


# DDGS/LinkedIn search snippets frequently come pre-structured as
# "· Experience: X · Education: Y · Location: Z · ...". When that's present,
# "Experience:" is direct, deterministic evidence of the person's employer —
# far more reliable than an LLM guess, and critically, distinct from
# "Education:" (alma mater), which must never be used as company_name.
_EXPERIENCE_FIELD_RE = re.compile(r"Experience:\s*([^·\n]+?)\s*(?:·|$)", re.I)


def extract_company_from_snippet(text: str) -> str:
    """Deterministic company-name extraction from a structured "Experience:"
    field in a search snippet/summary. Returns "" if no such field is
    present or the captured text isn't a valid bare company name — callers
    should fall back to extract_company_from_at_pattern() or LLM extraction
    (or leave blank) in that case.
    """
    m = _EXPERIENCE_FIELD_RE.search(text or "")
    if not m:
        return ""
    candidate = clean_company_name(m.group(1))
    if is_valid_company_name(candidate):
        return candidate
    return ""


# Deliberately narrow trigger set ("at"/"@"/"of" only — not "for"/"with"/
# "founded"/"leading"/etc, which risk snagging a capitalized non-company
# phrase, e.g. "with Fortune 500 companies"). Catches the common LinkedIn
# headline/summary phrasing "<Title> at <Company>" or "<Title> of <Company>"
# even without a structured "Experience:" field, e.g. "CTO at Audacix,
# makers of Cyber Chief & Qsome" -> "Audacix". The captured run of
# Title-Case-ish words stops at the first lowercase word or punctuation, so
# a generic phrase like "Founder of an AI SaaS startup" (lowercase "an"
# right after "of") is correctly left unmatched instead of guessing.


# Text immediately preceding an "at/of X" match that signals X is where the
# person studies/studied, not where they work — "Junior MIS student at
# Rochester Institute of Technology" should never yield "Rochester
# Institute" (or, from a second match further into the same phrase,
# "Technology") as a company, even for an institute name that doesn't
# happen to contain "university"/"college". The whole enrollment clause
# (trigger word through the institution name, including a trailing "of X"
# like "... Institute of Technology") is stripped out before the company
# regex ever runs over the text, rather than checked match-by-match — that
# way a cascading second match inside the same clause can't slip through.
_EDU_ENROLLMENT_CLAUSE_RE = re.compile(
    r"\b(?:student|studying|studies|attending|enrolled|alumnus|alumna|alumni)\b"
    r"[\s\S]{0,80}?(?:University|College|Institute|School|Academy)"
    r"(?:\s+of\s+[A-Z][A-Za-z]+)?",
    re.I,
)

# Common corporate function/department words that frequently follow "of" in
# a headline ("VP of Sales", "Head of Product", "Director of Operations")
# without being a company at all — the actual employer in that phrasing
# almost always follows via "at <Company>" instead, e.g. "VP of Sales at
# Salesforce". Only relevant to the "of" pattern; "at"/"@" are tried first
# and don't need this guard.
_FUNCTION_WORD_STOPLIST = {
    "sales", "marketing", "engineering", "product", "operations", "design",
    "finance", "legal", "hr", "growth", "partnerships", "people", "talent",
    "strategy", "data", "security", "support", "success", "research",
    "development", "business development", "customer success",
}

_AT_COMPANY_RE = re.compile(
    r"(?:\bat\b|@)\s+([A-Z][A-Za-z0-9&.'\-]*(?:\s+[A-Z][A-Za-z0-9&.'\-]*){0,4})"
)
_OF_COMPANY_RE = re.compile(
    r"\bof\s+([A-Z][A-Za-z0-9&.'\-]*(?:\s+[A-Z][A-Za-z0-9&.'\-]*){0,4})"
)


def extract_company_from_at_pattern(text: str) -> str:
    """Second deterministic pass, used when extract_company_from_snippet()
    (the "Experience:" field check) finds nothing. Free and reliable —
    unlike the LLM fallback, it can't hallucinate a company that isn't
    actually named in the text, only miss one that's phrased unusually.

    Tries "<Title> at/@ <Company>" first (the strongest, least ambiguous
    signal) across the whole text, and only falls back to "<Title> of
    <Company>" if that finds nothing — "of" is more ambiguous ("VP of
    Sales" names a department, not a company) so those candidates are also
    checked against a function-word stoplist. Reuses the same
    is_valid_company_name/is_non_company_org validation the LLM path uses,
    so a match like "of the United States" or "at Turiba Business School"
    (an alma mater, not an employer) is still rejected the same way it
    would be from any other source, and skips anything inside an education-
    enrollment clause regardless of how the institution's own name reads.
    """
    text = _EDU_ENROLLMENT_CLAUSE_RE.sub(" ", text or "")
    for pattern, check_stoplist in ((_AT_COMPANY_RE, False), (_OF_COMPANY_RE, True)):
        for m in pattern.finditer(text):
            raw = m.group(1).strip()
            if check_stoplist and raw.lower() in _FUNCTION_WORD_STOPLIST:
                continue
            candidate = clean_company_name(raw)
            if not candidate or candidate.lower() in _FUNCTION_WORD_STOPLIST:
                continue
            if is_valid_company_name(candidate) and not is_non_company_org(candidate):
                return candidate
    return ""


# Common LinkedIn-headline title words -- used only to gate
# extract_company_from_dash_pattern below (never used to *reject* a role,
# only to confirm the text before a dash genuinely reads like a job title
# before trusting whatever follows the dash as a company name).
_TITLE_CUE_WORDS = frozenset(
    {
        "founder", "co-founder", "cofounder", "ceo", "cto", "coo", "cgo",
        "cmo", "cfo", "president", "director", "partner", "manager", "vp",
        "head", "chief", "owner", "principal", "architect",
    }
)

_TITLE_DASH_RE = re.compile(
    r"^(?P<title>[A-Za-z][A-Za-z\s&/,\-]{1,60}?)\s+-\s+(?P<company>[A-Z][A-Za-z0-9&.'\- ]{1,60})$"
)


def extract_company_from_dash_pattern(text: str) -> str:
    """Deterministic extraction for the common "<Title> - <Company>"
    profile-headline format (e.g. "Founder & CEO - Makesbridge",
    "Co-Founder & CGO - Mathos AI") -- a format
    extract_company_from_at_pattern's "at"/"of" triggers don't cover at
    all, since there's no "at"/"of" word in it.

    Conservative on purpose: only fires when the text before the dash
    contains a recognizable job-title word (_TITLE_CUE_WORDS), so an
    arbitrary "X - Y" headline (a date range, a tagline, two unrelated
    clauses) isn't misread as "<role> - <company>". Reuses the same
    is_valid_company_name/is_non_company_org validation every other
    extractor in this module uses.
    """
    text = (text or "").strip()
    m = _TITLE_DASH_RE.match(text)
    if not m:
        return ""
    title_low = m.group("title").lower()
    if not any(w in title_low for w in _TITLE_CUE_WORDS):
        return ""
    candidate = clean_company_name(m.group("company"))
    if is_valid_company_name(candidate) and not is_non_company_org(candidate):
        return candidate
    return ""


def _title_company_regexes(sep: str) -> tuple["re.Pattern[str]", "re.Pattern[str]"]:
    sep_esc = re.escape(sep)
    title_then_company = re.compile(
        rf"^(?P<title>[A-Za-z][A-Za-z\s&/,\-]{{1,60}}?)\s*{sep_esc}\s*"
        rf"(?P<company>[A-Z][A-Za-z0-9&.'\- ]{{1,60}})$"
    )
    company_then_title = re.compile(
        rf"^(?P<company>[A-Z][A-Za-z0-9&.'\- ]{{1,60}}?)\s*{sep_esc}\s*"
        rf"(?P<title>[A-Za-z][A-Za-z\s&/,\-]{{1,60}})$"
    )
    return title_then_company, company_then_title


def extract_company_from_separator_pattern(
    text: str, *, separators: tuple[str, ...] = ("|", "\u00b7", ",")
) -> str:
    """Deterministic extraction for "<Title> <sep> <Company>" or
    "<Company> <sep> <Title>" profile-headline shapes not covered by
    extract_company_from_at_pattern (at/of) or extract_company_from_dash_
    pattern (" - "), e.g.:
      "CEO | Makesbridge"          "Makesbridge | CEO"
      "Hebbia, Founder & CEO"      "Founder & CEO, Hebbia"
      "Acme \u00b7 Founder"                 "Founder \u00b7 Acme"

    Tries each separator in both orderings, gated (like
    extract_company_from_dash_pattern) on a recognizable job-title cue
    word being present on the title side, so an arbitrary "X <sep> Y"
    pair isn't misread as company evidence. Reuses the same
    is_valid_company_name/is_non_company_org validation every other
    extractor in this module uses.
    """
    text = (text or "").strip()
    if not text:
        return ""
    for sep in separators:
        title_then_company, company_then_title = _title_company_regexes(sep)
        for pattern in (title_then_company, company_then_title):
            m = pattern.match(text)
            if not m:
                continue
            title_low = m.group("title").lower()
            if not any(w in title_low for w in _TITLE_CUE_WORDS):
                continue
            candidate = clean_company_name(m.group("company"))
            if not is_valid_company_name(candidate) or is_non_company_org(candidate):
                continue
            # For the "<Company> <sep> <Title>" ordering specifically, guard
            # against a mis-split "<Name> - <Title>" fragment being read as
            # the company (e.g. "Shelly Freeman - CEO | COO": the pipe
            # splits it into company="Shelly Freeman - CEO" / title="COO",
            # and "Shelly Freeman - CEO" is structurally a valid-looking
            # company string) -- if the candidate itself still contains an
            # embedded "Name - Title" pattern, it's a name/title fragment,
            # not a company.
            if pattern is company_then_title and _NAME_TITLE_PREFIX_RE.search(candidate):
                continue
            return candidate
    return ""


# ---------------------------------------------------------------------------
# Confidence-labeled company resolution -- the single entry point that ties
# every deterministic extractor above together into one ranked pipeline,
# so callers (email_discovery.py's domain resolution, the orchestrator's
# enrichment phase, ...) get a company name AND an honest label for how
# much to trust it, rather than a single opaque string. Never guesses;
# UNKNOWN means exactly that, not "assume no company".
# ---------------------------------------------------------------------------

COMPANY_CONFIDENCE_EXPLICIT = "EXPLICIT"
COMPANY_CONFIDENCE_STRONG_INFERRED = "STRONG_INFERRED"
COMPANY_CONFIDENCE_WEAK_INFERRED = "WEAK_INFERRED"
COMPANY_CONFIDENCE_UNKNOWN = "UNKNOWN"

COMPANY_CONFIDENCE_LEVELS = (
    COMPANY_CONFIDENCE_EXPLICIT,
    COMPANY_CONFIDENCE_STRONG_INFERRED,
    COMPANY_CONFIDENCE_WEAK_INFERRED,
    COMPANY_CONFIDENCE_UNKNOWN,
)


def resolve_company(row: InvestorRow) -> tuple[str, str]:
    """Best-effort, confidence-labeled company resolution from every
    deterministic evidence form available on `row`'s profile_title/summary/
    company_name fields -- without ever guessing. Returns (company_name,
    confidence), confidence one of COMPANY_CONFIDENCE_LEVELS, most to
    least trustworthy:

      EXPLICIT         row already carries a company_name that itself
                        passes validation (not a university/generic term).
      STRONG_INFERRED   recovered from a structured "Experience:" field
                        (extract_company_from_snippet) -- LinkedIn's own
                        structured data, not free-text phrasing.
      WEAK_INFERRED     recovered from headline/summary phrasing: "<Title>
                        at/of <Company>", "<Title> - <Company>", or a
                        pipe/comma/middot-separated headline. Still fully
                        deterministic and pattern-validated, just a lower-
                        trust source than a structured field.
      UNKNOWN           nothing could be confidently recovered; ("", UNKNOWN).

    Never returns a university, a bare generic industry term, or a
    malformed string at ANY confidence level -- every candidate this
    returns has already passed is_valid_company_name/is_non_company_org.
    Callers that need an even stricter "is this good enough to guess a
    domain from" bar should additionally check
    is_domain_guessable_company_name() on the result.
    """
    summary = row.get("summary", "") or ""
    title = row.get("profile_title", "") or ""

    existing = (row.get("company_name") or "").strip()
    if (
        existing
        and is_valid_company_name(existing)
        and not is_non_company_org(existing)
        and not _is_enrollment_mention(existing, f"{title} {summary}")
    ):
        return existing, COMPANY_CONFIDENCE_EXPLICIT

    strong = extract_company_from_snippet(summary)
    if strong:
        return strong, COMPANY_CONFIDENCE_STRONG_INFERRED

    combined = f"{title} {summary}"
    for extractor, source_text in (
        (extract_company_from_at_pattern, combined),
        (extract_company_from_dash_pattern, title),
        (extract_company_from_separator_pattern, title),
    ):
        weak = extractor(source_text)
        if weak and not _is_enrollment_mention(weak, combined):
            return weak, COMPANY_CONFIDENCE_WEAK_INFERRED

    return "", COMPANY_CONFIDENCE_UNKNOWN


def extract_email(text: str) -> str:
    for m in EMAIL_RE.findall(text or ""):
        low = m.lower()
        if any(x in low for x in ("example.com", "email.com", "domain.com", "sentry")):
            continue
        return m
    return ""


def extract_phone(text: str) -> str:
    m = PHONE_RE.search(text or "")
    return m.group(0).strip() if m else ""


def merge_investor_row(existing: InvestorRow, new: InvestorRow) -> InvestorRow:
    merged = dict(existing)
    new_source = new.get("source", "")
    existing_source = existing.get("source", "")
    for key, val in new.items():
        if val is None or val == "":
            continue
        cur = merged.get(key, "")
        if not cur:
            merged[key] = val
        elif key in ("summary", "profile_title", "industries"):
            prefer_new = len(str(val)) > len(str(cur))
            if (
                key == "summary"
                and new_source in DIRECTORY_SOURCES
                and existing_source == "ddgs_search"
            ):
                prefer_new = len(str(val)) >= len(str(cur))
            if prefer_new:
                merged[key] = val
        elif key == "email" and "@" in str(val) and "@" not in str(cur):
            merged[key] = val
        elif key == "phone" and str(val) and not str(cur):
            merged[key] = val
    li = normalize_linkedin(merged.get("linkedin_url", "") or new.get("linkedin_url", ""))
    if li:
        merged["linkedin_url"] = li
    return merged  # type: ignore[return-value]


def passes_quality_bar(row: InvestorRow, target: TargetConfig | None = None) -> bool:
    title = row.get("profile_title", "")
    summary = row.get("summary", "")
    name = row.get("name", "")
    loc = row.get("location", "")
    source = row.get("source", "")
    if not is_valid_person_name(name):
        return False
    if is_company_or_org_page(title, summary):
        return False
    if is_non_us_text(f"{title} {summary} {loc}"):
        return False
    if source in DISCOVERY_SOURCES and not matches_target_criteria(row, target):
        return False
    li = normalize_linkedin(row.get("linkedin_url", ""))
    if not li:
        return False
    return True


def set_dedup_memory(slugs: set[str]) -> None:
    """Replace the baseline LinkedIn-slug blocklist used by add_investor."""
    global _DEDUP_MEMORY
    _DEDUP_MEMORY = {s for s in slugs if s}


def dedup_memory_size() -> int:
    return len(_DEDUP_MEMORY)


def add_investor(investors: dict[str, InvestorRow], row: InvestorRow) -> bool:
    li = normalize_linkedin(row.get("linkedin_url", ""))
    if not li:
        return False
    slug = linkedin_slug(li)
    if not slug:
        return False
    cleaned = dict(row)
    cleaned["linkedin_url"] = li
    if slug in investors or slug in _DEDUP_MEMORY:
        return False
    investors[slug] = cleaned  # type: ignore[assignment]
    return True


def _slugs_from_linkedin_field(value: str) -> set[str]:
    li = normalize_linkedin(value or "")
    slug = linkedin_slug(li)
    return {slug} if slug else set()


def load_linkedin_slugs_from_csv(path: Path) -> set[str]:
    """Load all LinkedIn slugs from a CSV — no quality filter (memory must be complete)."""
    slugs: set[str] = set()
    if not path.exists():
        return slugs
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            slugs |= _slugs_from_linkedin_field(row.get("linkedin_url", "") or "")
    return slugs


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in zf.namelist():
        return []
    root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    out: list[str] = []
    for si in root.findall("m:si", _XLSX_NS):
        texts = [
            (t.text or "")
            for t in si.iter("{http://schemas.openxmlformats.org/spreadsheetml/2006/main}t")
        ]
        out.append("".join(texts))
    return out


def _xlsx_cell_value(cell: ET.Element, shared: list[str]) -> str:
    v = cell.find("m:v", _XLSX_NS)
    if v is None or v.text is None:
        return ""
    if cell.attrib.get("t") == "s":
        try:
            return shared[int(v.text)]
        except (ValueError, IndexError):
            return ""
    return v.text


def _xlsx_col_letters(ref: str) -> str:
    return "".join(c for c in ref if c.isalpha())


def load_linkedin_slugs_from_xlsx(path: Path) -> set[str]:
    """Read linkedin_url column from sheet1 via stdlib zip/xml (no openpyxl)."""
    slugs: set[str] = set()
    if not path.exists():
        return slugs
    try:
        with zipfile.ZipFile(path) as zf:
            shared = _xlsx_shared_strings(zf)
            sheet = ET.fromstring(zf.read("xl/worksheets/sheet1.xml"))
            rows = sheet.findall("m:sheetData/m:row", _XLSX_NS)
            if not rows:
                return slugs
            header: dict[str, str] = {}
            for cell in rows[0].findall("m:c", _XLSX_NS):
                col = _xlsx_col_letters(cell.attrib.get("r", ""))
                if col:
                    header[col] = _xlsx_cell_value(cell, shared)
            li_cols = [c for c, name in header.items() if name.strip().lower() == "linkedin_url"]
            if not li_cols:
                # Fallback: any cell that looks like a LinkedIn /in/ URL
                for row in rows[1:]:
                    for cell in row.findall("m:c", _XLSX_NS):
                        slugs |= _slugs_from_linkedin_field(_xlsx_cell_value(cell, shared))
                return slugs
            li_col = li_cols[0]
            for row in rows[1:]:
                for cell in row.findall("m:c", _XLSX_NS):
                    if _xlsx_col_letters(cell.attrib.get("r", "")) == li_col:
                        slugs |= _slugs_from_linkedin_field(_xlsx_cell_value(cell, shared))
                        break
    except Exception as exc:
        print(f"  warn: failed reading memory xlsx {path.name}: {exc}", file=sys.stderr)
    return slugs


def load_dedup_memory(paths: list[Path]) -> set[str]:
    """Union of LinkedIn slugs from baseline CSV/XLSX memory sheets."""
    slugs: set[str] = set()
    for path in paths:
        if not path.exists():
            print(f"  memory miss: {path.name} (not found)", file=sys.stderr)
            continue
        if path.suffix.lower() in {".xlsx", ".xlsm"}:
            got = load_linkedin_slugs_from_xlsx(path)
        else:
            got = load_linkedin_slugs_from_csv(path)
        print(f"  memory +{len(got)} slugs from {path.name}", file=sys.stderr)
        slugs |= got
    set_dedup_memory(slugs)
    print(f"  dedup memory ready: {len(slugs)} unique LinkedIn slugs", file=sys.stderr)
    return slugs


def dedupe_investors(investors: dict[str, InvestorRow]) -> dict[str, InvestorRow]:
    deduped: dict[str, InvestorRow] = {}
    for row in investors.values():
        li = normalize_linkedin(row.get("linkedin_url", ""))
        if not li:
            continue
        slug = linkedin_slug(li)
        if not slug:
            continue
        row = dict(row)
        row["linkedin_url"] = li
        if slug in deduped:
            deduped[slug] = merge_investor_row(deduped[slug], row)
        else:
            deduped[slug] = row  # type: ignore[assignment]
    return deduped


def load_existing_csv(path: Path, *, apply_quality: bool = False) -> dict[str, InvestorRow]:
    """Load investors from a CSV.

    apply_quality=False (default): keep every row with a valid LinkedIn slug so
    resume / 'include everything' does not silently drop prior work.
    """
    investors: dict[str, InvestorRow] = {}
    if not path.exists():
        return investors
    dropped = 0
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if apply_quality and not passes_quality_bar(row):  # type: ignore[arg-type]
                dropped += 1
                continue
            li = normalize_linkedin(row.get("linkedin_url", ""))
            slug = linkedin_slug(li)
            if not slug:
                continue
            row = dict(row)
            row["linkedin_url"] = li
            if slug in investors:
                investors[slug] = merge_investor_row(investors[slug], row)  # type: ignore[arg-type]
            else:
                investors[slug] = row  # type: ignore[assignment]
    if dropped:
        print(f"  filtered {dropped} low-quality rows on load", file=sys.stderr)
    return investors


def save_csv(path: Path, investors: dict[str, InvestorRow], limit: int | None = None) -> int:
    import sys

    rows = sorted(investors.values(), key=lambda r: (r.get("name") or "").lower())
    if limit:
        rows = rows[:limit]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        w.writerows(rows)
    return len(rows)
