"""Crawl4AI Playwright crawl adapter for JS-heavy directory seeds."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys

from openai import OpenAI

from ..config import DirectorySeed, FAILURES_LOG, load_directory_seeds
from ..linkedin_harvest import harvest_linkedin_from_text
from ..llm import extract_investors_from_markdown
from ..models import InvestorRow
from ..quality import add_investor
from .http_crawl import http_crawl_markdown


def _safe_print(msg: str) -> None:
    text = msg.encode("ascii", "replace").decode("ascii")
    print(text, file=sys.stderr)


def _log_failure(seed: DirectorySeed, error: str) -> None:
    FAILURES_LOG.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"engine": "crawl4ai", "seed": seed.name, "url": seed.url, "error": error},
        ensure_ascii=True,
    )
    with FAILURES_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _url_allowed(url: str, seed: DirectorySeed) -> bool:
    if not seed.include_patterns:
        return True
    return any(re.search(pat, url, re.I) for pat in seed.include_patterns)


async def _crawl_async(seed: DirectorySeed) -> str:
    from crawl4ai import AsyncWebCrawler, BrowserConfig, CrawlerRunConfig, CacheMode

    # Quiet Windows cp1252 console crashes on arrows / fancy unicode in libs
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

    browser_cfg = BrowserConfig(headless=True, verbose=False)
    run_cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, word_count_threshold=10)

    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(seed.url, 0)]
    markdown_parts: list[str] = []

    async with AsyncWebCrawler(config=browser_cfg) as crawler:
        while queue and len(visited) < seed.max_pages:
            url, depth = queue.pop(0)
            if url in visited or depth > seed.max_depth:
                continue
            # Allow seed URL even if patterns would exclude it
            if url != seed.url and not _url_allowed(url, seed):
                continue
            visited.add(url)

            result = await crawler.arun(url=url, config=run_cfg)
            if not result.success:
                continue
            md = result.markdown or ""
            if md.strip():
                markdown_parts.append(md)

            if depth < seed.max_depth:
                for link in result.links.get("internal", []) if result.links else []:
                    href = link.get("href") if isinstance(link, dict) else str(link)
                    if href and href not in visited:
                        queue.append((href, depth + 1))

    return "\n\n".join(markdown_parts)


def crawl_seed_markdown(seed: DirectorySeed) -> str:
    try:
        return asyncio.run(_crawl_async(seed))
    except Exception as exc:
        _safe_print(f"  crawl4ai primary fail {seed.name}: {exc}; HTTP fallback")
        return http_crawl_markdown(seed)


def run_crawl4ai_phase(
    investors: dict[str, InvestorRow],
    *,
    llm: OpenAI | None,
    target: int,
    skip_llm_extract: bool,
) -> int:
    seeds = load_directory_seeds("crawl4ai")
    if not seeds:
        _safe_print("  no crawl4ai seeds configured")
        return 0

    added = 0
    for seed in seeds:
        if len(investors) >= target:
            break
        _safe_print(f"  crawl4ai: {seed.name} ({seed.url})")
        try:
            markdown = crawl_seed_markdown(seed)
        except Exception as exc:
            msg = str(exc).encode("ascii", "replace").decode("ascii")
            _safe_print(f"  crawl4ai fail {seed.name}: {msg}")
            _log_failure(seed, msg)
            continue

        if not markdown.strip():
            continue

        for row in harvest_linkedin_from_text(
            markdown, source="crawl4ai_directory", seed_name=seed.name
        ):
            if add_investor(investors, row):
                added += 1
            if len(investors) >= target:
                break

        if skip_llm_extract or not llm:
            continue

        rows = extract_investors_from_markdown(
            llm, markdown, seed_name=seed.name, source="crawl4ai_directory"
        )
        for row in rows:
            if add_investor(investors, row):
                added += 1
            if len(investors) >= target:
                break

    _safe_print(f"  +{added} from crawl4ai (total {len(investors)})")
    return added
