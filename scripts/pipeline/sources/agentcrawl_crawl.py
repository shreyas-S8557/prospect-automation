"""agentcrawl directory crawl adapter with HTTP fallback."""

from __future__ import annotations

import json
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
    line = json.dumps(
        {"engine": "agentcrawl", "seed": seed.name, "url": seed.url, "error": error},
        ensure_ascii=True,
    )
    with FAILURES_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _safe(s: object) -> str:
    return str(s).encode("ascii", "replace").decode("ascii")


def crawl_seed_markdown(seed: DirectorySeed) -> str:
    try:
        from agentcrawl import AgentCrawl
    except ImportError:
        return http_crawl_markdown(seed)

    try:
        crawler = AgentCrawl({"fetcher": "http"})
        result = crawler.crawl(seed.url, max_pages=seed.max_pages, max_depth=seed.max_depth)
    except Exception as exc:
        print(f"  agentcrawl exception {seed.name}: {_safe(exc)}; HTTP fallback", file=sys.stderr)
        return http_crawl_markdown(seed)

    if hasattr(result, "ok") and result.ok is False:
        err = getattr(result, "error_type", None) or getattr(result, "error", None) or "unknown"
        print(f"  agentcrawl {seed.name} not ok: {_safe(err)}; HTTP fallback", file=sys.stderr)
        return http_crawl_markdown(seed)

    documents = []
    if isinstance(result, dict):
        documents = result.get("documents") or result.get("pages") or []
    elif hasattr(result, "documents"):
        documents = result.documents or []

    parts: list[str] = []
    for doc in documents:
        if isinstance(doc, dict):
            md = doc.get("markdown") or doc.get("content") or doc.get("text") or ""
        else:
            md = (
                getattr(doc, "markdown", "")
                or getattr(doc, "content", "")
                or getattr(doc, "text", "")
            )
        if md:
            parts.append(str(md))
    if not parts and hasattr(result, "markdown") and result.markdown:
        parts.append(str(result.markdown))
    if not parts:
        return http_crawl_markdown(seed)
    return "\n\n".join(parts)


def run_agentcrawl_phase(
    investors: dict[str, InvestorRow],
    *,
    llm: OpenAI | None,
    target: int,
    skip_llm_extract: bool,
) -> int:
    seeds = load_directory_seeds("agentcrawl")
    if not seeds:
        print("  no agentcrawl seeds configured", file=sys.stderr)
        return 0

    added = 0
    for seed in seeds:
        if len(investors) >= target:
            break
        print(f"  agentcrawl: {seed.name} ({seed.url})", file=sys.stderr)
        try:
            markdown = crawl_seed_markdown(seed)
        except Exception as exc:
            msg = _safe(exc)
            print(f"  agentcrawl fail {seed.name}: {msg}", file=sys.stderr)
            _log_failure(seed, msg)
            continue

        if not markdown.strip():
            continue

        for row in harvest_linkedin_from_text(
            markdown, source="agentcrawl_directory", seed_name=seed.name
        ):
            if add_investor(investors, row):
                added += 1
            if len(investors) >= target:
                break

        if skip_llm_extract or not llm:
            continue

        rows = extract_investors_from_markdown(
            llm, markdown, seed_name=seed.name, source="agentcrawl_directory"
        )
        for row in rows:
            if add_investor(investors, row):
                added += 1
            if len(investors) >= target:
                break

    print(f"  +{added} from agentcrawl (total {len(investors)})", file=sys.stderr)
    return added
