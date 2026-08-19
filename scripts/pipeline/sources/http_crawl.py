"""Lightweight HTTP BFS crawl — used when webclaw/agentcrawl/API keys unavailable."""

from __future__ import annotations

import re
from collections import deque
from urllib.parse import urljoin, urlparse

import httpx

from ..config import DirectorySeed

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
HREF_RE = re.compile(r"""href=["']([^"']+)["']""", re.I)


def _same_site(base: str, url: str) -> bool:
    try:
        return urlparse(base).netloc.lower() == urlparse(url).netloc.lower()
    except Exception:
        return False


def http_crawl_markdown(seed: DirectorySeed, client: httpx.Client | None = None) -> str:
    own = client is None
    http = client or httpx.Client(
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"},
    )
    visited: set[str] = set()
    queue: deque[tuple[str, int]] = deque([(seed.url, 0)])
    parts: list[str] = []
    try:
        while queue and len(visited) < seed.max_pages:
            url, depth = queue.popleft()
            if url in visited or depth > seed.max_depth:
                continue
            visited.add(url)
            try:
                resp = http.get(url)
                if resp.status_code >= 400:
                    continue
                text = resp.text
            except Exception:
                continue
            parts.append(f"# {url}\n\n{text}")
            if depth >= seed.max_depth:
                continue
            for href in HREF_RE.findall(text):
                abs_url = urljoin(url, href.split("#")[0])
                if not abs_url.startswith("http"):
                    continue
                if not _same_site(seed.url, abs_url):
                    continue
                if seed.include_patterns and not any(
                    re.search(p, abs_url, re.I) for p in seed.include_patterns
                ):
                    # still allow root-depth children without patterns when depth==0 child
                    if depth > 0:
                        continue
                if abs_url not in visited:
                    queue.append((abs_url, depth + 1))
    finally:
        if own:
            http.close()
    return "\n\n".join(parts)
