#!/usr/bin/env python3
"""Re-classify industries for angel investor CSV using FreeLLMAPI."""

from __future__ import annotations

import csv
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from tqdm import tqdm

_SCRIPTS = Path(__file__).resolve().parent
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from pipeline.llm import classify_industries_llm, get_llm_client  # noqa: E402

ROOT = _SCRIPTS.parent
CSV_PATH = ROOT / "data" / "us_angel_investors_1000.csv"
BATCH_SIZE = 10

DISCOVERY_SOURCES = {
    "exa_people",
    "ddgs_search",
    "webclaw_directory",
    "agentcrawl_directory",
    "crawl4ai_directory",
}


def load_env() -> None:
    load_dotenv(ROOT / ".env")
    load_dotenv(_SCRIPTS / ".env")
    import os

    hermes = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / ".env"
    if hermes.exists():
        load_dotenv(hermes)


def clean_location(loc: str) -> str:
    loc = (loc or "").strip()
    loc = re.sub(r"^\s*in\s+", "", loc, flags=re.I)
    return loc.strip() or "United States"


def main() -> None:
    load_env()
    client = get_llm_client()
    rows = list(csv.DictReader(CSV_PATH.open(encoding="utf-8")))
    need = [
        r
        for r in rows
        if r.get("source") in DISCOVERY_SOURCES
        and (not r.get("industries") or r["industries"] == "Generalist")
    ]
    print(f"Reclassifying {len(need)} rows...", file=sys.stderr)
    for i in tqdm(range(0, len(need), BATCH_SIZE)):
        batch = need[i : i + BATCH_SIZE]
        classify_industries_llm(client, batch)
        time.sleep(0.4)

    for row in rows:
        row["location"] = clean_location(row.get("location", ""))

    fields = list(rows[0].keys()) if rows else []
    with CSV_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    generalist = sum(1 for r in rows if r.get("industries") == "Generalist")
    empty = sum(1 for r in rows if not r.get("industries"))
    print(f"Done. Generalist: {generalist}, empty: {empty}", file=sys.stderr)


if __name__ == "__main__":
    main()
