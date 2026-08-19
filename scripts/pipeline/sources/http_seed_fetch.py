"""Fast HTTP fetch of seed pages + LinkedIn /in/ URL harvest (no LinkedIn login)."""

from __future__ import annotations

import sys

import httpx
from openai import OpenAI

from ..config import load_directory_seeds
from ..linkedin_harvest import harvest_linkedin_from_text
from ..llm import extract_investors_from_markdown
from ..models import InvestorRow
from ..quality import add_investor

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)


def run_http_seed_phase(
    investors: dict[str, InvestorRow],
    *,
    http: httpx.Client,
    llm: OpenAI | None,
    target: int,
    skip_llm_extract: bool,
) -> int:
    seeds = load_directory_seeds()
    if not seeds:
        return 0

    added = 0
    print(f"HTTP seeding {len(seeds)} directory pages for LinkedIn /in/ URLs...", file=sys.stderr)

    for seed in seeds:
        if len(investors) >= target:
            break
        try:
            resp = http.get(seed.url, headers={"User-Agent": UA}, timeout=30)
            resp.raise_for_status()
            text = resp.text
        except Exception as exc:
            print(f"  http seed fail {seed.name}: {exc}", file=sys.stderr)
            continue

        source = f"{seed.engine}_directory"
        harvested = harvest_linkedin_from_text(text, source=source, seed_name=seed.name)
        for row in harvested:
            if add_investor(investors, row):
                added += 1
            if len(investors) >= target:
                break

        if llm and not skip_llm_extract and len(investors) < target:
            for row in extract_investors_from_markdown(
                llm, text[:12000], seed_name=seed.name, source=source
            ):
                if add_investor(investors, row):
                    added += 1
                if len(investors) >= target:
                    break

        print(
            f"  http {seed.name}: harvested={len(harvested)} total={len(investors)}",
            file=sys.stderr,
        )

    print(f"  +{added} from HTTP seed LinkedIn harvest (total {len(investors)})", file=sys.stderr)
    return added
