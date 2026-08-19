#!/usr/bin/env python3
"""
Collect US-based angel investors with LinkedIn profiles.

Multi-source Exa-free pipeline:
  - Fund-Flow public CSV
  - ddgs DuckDuckGo search (site:linkedin.com/in)
  - webclaw / agentcrawl / crawl4ai directory crawls
  - FreeLLMAPI for parsing, extraction, and industry classification
  - Optional legacy Exa via --use-exa
"""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from pipeline.orchestrator import build_arg_parser, run_pipeline


def main() -> None:
    parser = build_arg_parser()
    args = parser.parse_args()
    raise SystemExit(run_pipeline(args))


if __name__ == "__main__":
    main()
