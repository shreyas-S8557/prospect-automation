"""DuckDuckGo-library LinkedIn discovery (Yahoo backend — DDG HTML times out)."""

from __future__ import annotations

import re
import sys
import time
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

from openai import OpenAI

from ..config import (
    DISCOVERY_DELAY_SEC,
    DISCOVERY_MAX_ACCEPT_PER_QUERY,
    DISCOVERY_NUM_RESULTS,
    get_active_target,
    shuffled_queries,
)
from ..llm import parse_search_hit_llm
from ..models import InvestorRow
from ..quality import (
    add_investor,
    decontaminate_hit_text,
    extract_email,
    extract_location,
    extract_phone,
    is_company_or_org_page,
    is_contaminated_hit,
    is_valid_person_name,
    matches_target_criteria,
    normalize_linkedin,
)
from ..target_config import TargetConfig

# NOTE: "bing" is NOT a valid backend in ddgs>=9 (confirmed via live run:
# "bing - backends do not exist or are disabled. Available: brave,
# duckduckgo, google, grokipedia, mojeek, startpage, wikipedia, yahoo,
# yandex"). Passing it makes the ddgs library silently fall back to
# backend="auto", which then serially probes EVERY available backend on
# EVERY query (8+ extra HTTP round-trips per query, several of which 403/429
# in practice) -- this alone was turning ~1-2s/query into 10-15s+/query.
# Yahoo alone reliably returns real /in/ URLs (see ddgs_search.py history).
# Keep one additional, real, working backend as a fallback for when yahoo
# itself has a bad day, but never reference a backend name that doesn't
# exist in the installed ddgs version.
DISCOVERY_BACKENDS = ("yahoo", "duckduckgo")


def _unwrap_href(href: str) -> str:
    """Pull a LinkedIn /in/ URL out of Bing redirect junk when present."""
    if not href:
        return ""
    if "linkedin.com/in/" in href.lower():
        return href
    try:
        parsed = urlparse(href)
        qs = parse_qs(parsed.query)
        for key in ("u", "url", "r", "ru", "q"):
            for val in qs.get(key, []):
                decoded = unquote(val)
                if "linkedin.com/in/" in decoded.lower():
                    return decoded
    except Exception:
        pass
    m = re.search(r"https?%3A%2F%2F(?:www\.)?linkedin\.com%2Fin%2F[A-Za-z0-9\-_%]+", href, re.I)
    if m:
        return unquote(m.group(0))
    return href


def ddgs_search(
    query: str, max_results: int, *, linkedin_only: bool = True
) -> list[dict[str, str]]:
    try:
        from ddgs import DDGS
    except ImportError as exc:
        raise ImportError("pip install ddgs") from exc

    ddg_query = f"site:linkedin.com/in {query}" if linkedin_only else query
    last_err: Exception | None = None

    for backend in DISCOVERY_BACKENDS:
        try:
            with DDGS() as ddgs:
                raw = list(ddgs.text(ddg_query, backend=backend, max_results=max_results))
            hits: list[dict[str, str]] = []
            for r in raw:
                href = _unwrap_href(r.get("href", "") or r.get("link", ""))
                if linkedin_only and not normalize_linkedin(href):
                    continue
                hits.append(
                    {
                        "href": href,
                        "title": r.get("title", "") or "",
                        "body": r.get("body", "") or r.get("description", "") or "",
                    }
                )
            if hits:
                return hits
        except Exception as exc:
            last_err = exc
            continue

    if last_err:
        print(f"  ddgs warn: {query[:70]}... -> {last_err}", file=sys.stderr)
    return []


def parse_search_hit(
    hit: dict[str, str],
    source: str = "ddgs_search",
    target: TargetConfig | None = None,
    *,
    require_target_match: bool = True,
) -> InvestorRow | None:
    """Parse one search hit into an InvestorRow, gated by matches_target_criteria().

    `target` defaults to the process-wide active TargetConfig (CPA preset if
    none was ever set via config.set_active_target) — the same backward-
    compatible default that shuffled_queries() uses, so existing zero-arg
    call sites (this function's own tests included) keep working unchanged.

    `require_target_match` defaults to True (unchanged production behaviour:
    a hit must satisfy matches_target_criteria() to be returned at all).
    Set to False only for diagnostics/tooling that want to inspect whether a
    hit is a *structurally* clean, single-person candidate (name/location/
    not-a-company-page/not-contaminated) independent of whether it happens
    to carry enough snippet-only evidence for the target's industries/
    keywords dimensions — useful for telling apart "this is noisy,
    multi-person garbage" from "this is one real person who may just need
    the enrichment step (task 6) to confirm industry/keyword relevance."
    """
    url = hit.get("href", "")
    li = normalize_linkedin(url)
    if not li:
        return None

    title = (hit.get("title") or "").strip()
    text = (hit.get("body") or "").strip()

    if is_company_or_org_page(title, text):
        return None

    # When the title or snippet glues more than one LinkedIn profile
    # together — e.g. Yahoo/Bing occasionally concatenates several
    # unrelated "Name - Title - Company | LinkedIn" results into one hit —
    # first try to salvage just the FIRST person's own segment (almost
    # always the target: this hit's own /in/ URL, with a second, unrelated
    # hit concatenated onto the end of it). decontaminate_hit_text() is a
    # no-op when no contamination boundary is found, so this changes
    # nothing for the overwhelming majority of already-clean hits. If
    # contamination is still detected even after truncating (e.g. two
    # "Experience:" blocks, which has no reliable single split point),
    # reject the whole result exactly as before — there is still no safe
    # way to know which sentences belong to *this* /in/ URL in that case.
    # This must run before any evidence (including location) is pulled
    # from `text`/`title`.
    title = decontaminate_hit_text(title)
    text = decontaminate_hit_text(text)
    if is_contaminated_hit(title, text):
        return None

    name = title.split("|")[0].split(" - ")[0].strip()
    name = re.sub(r"^#+\s*", "", name)
    # Yahoo sometimes glues two titles together
    name = re.split(r"LinkedIn", name, maxsplit=1)[0].strip()
    if not is_valid_person_name(name):
        return None

    loc = extract_location(text, title)
    if not loc:
        return None

    candidate: InvestorRow = {
        "name": name,
        "location": loc,
        "linkedin_url": li,
        "profile_title": title[:500],
        "summary": text[:2000].strip(),
        "industries": "",
        "email": extract_email(text),
        "phone": extract_phone(text),
        "source": source,
    }
    active_target = target if target is not None else get_active_target()
    if require_target_match and not matches_target_criteria(candidate, active_target):
        return None

    return candidate


def run_ddgs_phase(
    investors: dict[str, InvestorRow],
    *,
    llm: OpenAI | None,
    target: int,
    skip_llm_extract: bool,
    on_checkpoint: Callable[[], None] | None = None,
    on_candidate: Callable[[InvestorRow], None] | None = None,
    on_query_progress: Callable[[int, int], None] | None = None,
) -> int:
    """`on_candidate`, when given, is called synchronously with each freshly
    accepted InvestorRow the moment it's added -- this is what lets a caller
    (the backend's discovery_service) persist candidates to the database and
    report live progress incrementally instead of waiting for the whole
    (potentially multi-minute) discovery run to finish. It must never raise:
    a caller-side persistence failure must not be allowed to take down
    discovery itself, so it's called inside a best-effort try/except here.
    """
    added = 0
    since_checkpoint = 0
    queries = shuffled_queries()
    empty_streak = 0

    print(
        f"Running LinkedIn discovery via ddgs backends={DISCOVERY_BACKENDS} "
        f"on {len(queries)} queries (max {DISCOVERY_MAX_ACCEPT_PER_QUERY}/query)...",
        file=sys.stderr,
    )

    for i, query in enumerate(queries, 1):
        if len(investors) >= target:
            break
        if on_query_progress is not None:
            try:
                on_query_progress(i, len(queries))
            except Exception as exc:  # noqa: BLE001
                print(f"  on_query_progress warn: {exc}", file=sys.stderr)

        results = ddgs_search(query, DISCOVERY_NUM_RESULTS)
        accepted = 0
        for hit in results:
            if accepted >= DISCOVERY_MAX_ACCEPT_PER_QUERY:
                break
            parsed = parse_search_hit(hit)
            if not parsed and llm and not skip_llm_extract:
                parsed = parse_search_hit_llm(
                    llm,
                    url=hit.get("href", ""),
                    title=hit.get("title", ""),
                    body=hit.get("body", ""),
                )
            if parsed and add_investor(investors, parsed):
                added += 1
                accepted += 1
                since_checkpoint += 1
                if on_candidate is not None:
                    try:
                        on_candidate(parsed)
                    except Exception as exc:  # noqa: BLE001 -- never kill discovery for a persistence hiccup
                        print(f"  on_candidate warn: {exc}", file=sys.stderr)
            if len(investors) >= target:
                break

        if accepted == 0 and not results:
            empty_streak += 1
        else:
            empty_streak = 0

        if i % 25 == 0 or accepted:
            print(
                f"  ddgs {i}/{len(queries)} +{accepted} this query "
                f"(added={added} total={len(investors)})",
                file=sys.stderr,
            )

        # If the network is dead, don't burn hours — directories still run after
        if empty_streak >= 40:
            print(
                f"  ddgs: {empty_streak} empty queries in a row — stopping LinkedIn search early",
                file=sys.stderr,
            )
            break

        if since_checkpoint >= 75 and on_checkpoint:
            on_checkpoint()
            since_checkpoint = 0
        time.sleep(DISCOVERY_DELAY_SEC)

    print(f"  +{added} from ddgs LinkedIn (total {len(investors)})", file=sys.stderr)
    return added
