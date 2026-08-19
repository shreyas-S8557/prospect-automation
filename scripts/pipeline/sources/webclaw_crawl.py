"""Directory crawl via HTTP BFS, or Webclaw cloud API if WEBCLAW_API_KEY is set."""

from __future__ import annotations

import json
import os
import sys

from openai import OpenAI

from ..config import DirectorySeed, FAILURES_LOG, load_directory_seeds
from ..linkedin_harvest import harvest_linkedin_from_text
from ..llm import extract_investors_from_markdown
from ..models import InvestorRow
from ..quality import add_investor
from .http_crawl import http_crawl_markdown


def _log_failure(seed: DirectorySeed, error: str) -> None:
    FAILURES_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"engine": "webclaw", "seed": seed.name, "url": seed.url, "error": error})
    with FAILURES_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def crawl_seed_markdown(seed: DirectorySeed) -> str:
    api_key = os.getenv("WEBCLAW_API_KEY", "").strip()
    if api_key:
        try:
            from webclaw import Webclaw  # type: ignore[import-untyped]

            client = Webclaw(api_key)
            result = client.crawl(
                seed.url,
                depth=seed.max_depth,
                max_pages=seed.max_pages,
                formats=["markdown"],
            )
            if isinstance(result, str):
                return result
            if isinstance(result, dict):
                pages = result.get("pages") or result.get("data", {}).get("pages") or []
                parts = []
                for page in pages:
                    if isinstance(page, dict):
                        md = page.get("markdown") or page.get("content", "")
                        if md:
                            parts.append(str(md))
                return "\n\n".join(parts)
            return str(result)
        except Exception as exc:
            print(f"  webclaw API fail {seed.name}, falling back to HTTP: {exc}", file=sys.stderr)

    # Free path: local HTTP BFS (pip `webclaw` is cloud-API-only)
    return http_crawl_markdown(seed)


def run_webclaw_phase(
    investors: dict[str, InvestorRow],
    *,
    llm: OpenAI | None,
    target: int,
    skip_llm_extract: bool,
) -> int:
    seeds = load_directory_seeds("webclaw")
    if not seeds:
        print("  no webclaw seeds configured", file=sys.stderr)
        return 0

    added = 0
    for seed in seeds:
        if len(investors) >= target:
            break
        print(f"  webclaw/http crawl: {seed.name} ({seed.url})", file=sys.stderr)
        try:
            markdown = crawl_seed_markdown(seed)
        except Exception as exc:
            msg = str(exc).encode("ascii", "replace").decode("ascii")
            print(f"  webclaw fail {seed.name}: {msg}", file=sys.stderr)
            _log_failure(seed, msg)
            continue

        for row in harvest_linkedin_from_text(
            markdown, source="webclaw_directory", seed_name=seed.name
        ):
            if add_investor(investors, row):
                added += 1
            if len(investors) >= target:
                break

        if skip_llm_extract or not llm or not markdown.strip():
            continue

        rows = extract_investors_from_markdown(
            llm, markdown, seed_name=seed.name, source="webclaw_directory"
        )
        for row in rows:
            if add_investor(investors, row):
                added += 1
            if len(investors) >= target:
                break

    print(f"  +{added} from webclaw/http (total {len(investors)})", file=sys.stderr)
    return added
