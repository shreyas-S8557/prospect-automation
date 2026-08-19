"""Legacy Exa people search (optional --use-exa)."""

from __future__ import annotations

import sys
import time
from typing import Callable

import httpx
from openai import OpenAI

from ..config import (
    DISCOVERY_DELAY_SEC,
    DISCOVERY_MAX_ACCEPT_PER_QUERY,
    DISCOVERY_NUM_RESULTS,
    get_active_target,
    shuffled_queries,
)
from ..models import InvestorRow
from ..quality import (
    add_investor,
    extract_email,
    extract_location,
    extract_phone,
    is_company_or_org_page,
    is_valid_person_name,
    matches_target_criteria,
    normalize_linkedin,
)
from ..target_config import TargetConfig


def exa_search(
    client: httpx.Client, api_key: str, query: str, num: int
) -> tuple[list[dict], str | None]:
    try:
        resp = client.post(
            "https://api.exa.ai/search",
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json={
                "query": query,
                "type": "auto",
                "numResults": num,
                "category": "people",
                "includeDomains": ["linkedin.com", "www.linkedin.com"],
                "contents": {"text": {"maxCharacters": 3500}},
            },
            timeout=60,
        )
        if resp.status_code == 402:
            return [], "payment_required"
        resp.raise_for_status()
        return resp.json().get("results", []), None
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 402:
            return [], "payment_required"
        print(f"  exa warn: {query[:70]}... -> {exc}", file=sys.stderr)
        return [], str(exc)
    except Exception as exc:
        print(f"  exa warn: {query[:70]}... -> {exc}", file=sys.stderr)
        return [], str(exc)


def parse_exa_result(r: dict, target: TargetConfig | None = None) -> InvestorRow | None:
    """Parse one Exa result into an InvestorRow, gated by matches_target_criteria().

    `target` defaults to the process-wide active TargetConfig, same
    backward-compatible pattern as ddgs_search.parse_search_hit.
    """
    url = r.get("url", "")
    li = normalize_linkedin(url)
    if not li:
        return None

    title = (r.get("title") or "").strip()
    text = (r.get("text") or "")
    if isinstance(r.get("highlights"), list):
        text += " " + " ".join(r["highlights"])
    elif r.get("highlights"):
        text += " " + str(r["highlights"])

    if is_company_or_org_page(title, text):
        return None

    name = title.split("|")[0].split(" - ")[0].strip()
    import re

    name = re.sub(r"^#+\s*", "", name)
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
        "source": "exa_people",
    }
    active_target = target if target is not None else get_active_target()
    if not matches_target_criteria(candidate, active_target):
        return None

    return candidate


def run_exa_phase(
    investors: dict[str, InvestorRow],
    *,
    http: httpx.Client,
    exa_key: str,
    target: int,
    on_checkpoint: Callable[[], None] | None = None,
) -> int:
    added = 0
    since_checkpoint = 0
    queries = shuffled_queries()
    payment_blocked = False

    print(
        f"Running Exa on {len(queries)} queries (max {DISCOVERY_MAX_ACCEPT_PER_QUERY}/query)...",
        file=sys.stderr,
    )

    for query in queries:
        if len(investors) >= target or payment_blocked:
            break
        results, err = exa_search(http, exa_key, query, DISCOVERY_NUM_RESULTS)
        if err == "payment_required":
            print("Exa credits exhausted (402). Stopping discovery.", file=sys.stderr)
            payment_blocked = True
            break
        accepted = 0
        for r in results:
            if accepted >= DISCOVERY_MAX_ACCEPT_PER_QUERY:
                break
            parsed = parse_exa_result(r)
            if parsed and add_investor(investors, parsed):
                added += 1
                accepted += 1
                since_checkpoint += 1
            if len(investors) >= target:
                break
        if since_checkpoint >= 75 and on_checkpoint:
            on_checkpoint()
            since_checkpoint = 0
        time.sleep(DISCOVERY_DELAY_SEC)

    print(f"  +{added} from Exa (total {len(investors)})", file=sys.stderr)
    return added
