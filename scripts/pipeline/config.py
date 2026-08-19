"""Pipeline configuration, env loading, and discovery query builders."""

from __future__ import annotations

import os
import random
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "data" / "us_cpa_partners_1000.csv"
OUTPUT_V2 = ROOT / "data" / "us_cpa_partners_v2.csv"
# Baseline for cross-run LinkedIn dedup (compare-only; not written to output)
MEMORY_PATH = ROOT / "data" / "us_cpa_partners_unique_clean.csv"
MEMORY_PATHS = [MEMORY_PATH]
SEEDS_PATH = ROOT / "data" / "cpa_directory_seeds.yaml"
CITIES_PATH = ROOT / "data" / "us_cities.txt"
FAILURES_LOG = ROOT / "data" / "crawl_failures.jsonl"

DEFAULT_BASE_URL = "https://freellmapiserver-production-df6f.up.railway.app/v1"
TARGET = 5000
DISCOVERY_NUM_RESULTS = 20
DISCOVERY_MAX_ACCEPT_PER_QUERY = 12
DISCOVERY_DELAY_SEC = 0.25
CHECKPOINT_EVERY = 75
QUERY_SHUFFLE_SEED = 42

# Legacy fund-flow CSV source (angel-investor specific) — left unused for the
# CPA-firm ICP; the "fund_flow" phase can be dropped from ALL_PHASES/parse_phases
# for this run, or left as a no-op if the file doesn't resolve.
FUND_FLOW_CSV_URL = (
    "https://raw.githubusercontent.com/Dessiidoo/Fund-Flow-AI/main/"
    "attached_assets/Fund_Database-Angel_Investors_1766572251362.csv"
)

US_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado",
    "Connecticut", "Delaware", "Florida", "Georgia", "Hawaii", "Idaho",
    "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky", "Louisiana",
    "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota",
    "Mississippi", "Missouri", "Montana", "Nebraska", "Nevada",
    "New Hampshire", "New Jersey", "New Mexico", "New York",
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon",
    "Pennsylvania", "Rhode Island", "South Carolina", "South Dakota",
    "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming", "District of Columbia",
]

# Deprecated: CPA_TITLES used to be the only title list the discovery engine
# knew about. Kept as an alias to the preset's titles so any external code
# still importing CPA_TITLES directly doesn't break. New code should use
# TargetConfig(titles=[...]) or CPA_PARTNER_PRESET from target_config.py.
def _cpa_titles() -> list[str]:
    from .target_config import CPA_PARTNER_PRESET

    return list(CPA_PARTNER_PRESET.titles)


def __getattr__(name: str):  # PEP 562 module-level lazy attribute; no eager
    # circular import at module load time (target_config doesn't exist yet
    # while config.py is still being imported by it).
    if name == "CPA_TITLES":
        return _cpa_titles()
    raise AttributeError(name)


# Fallback if data/us_cities.txt is missing
_US_CITIES_FALLBACK = [
    "San Francisco", "New York City", "Austin", "Boston", "Seattle", "Los Angeles",
    "Miami", "Chicago", "Denver", "Atlanta", "Dallas", "Philadelphia", "Phoenix",
    "Portland", "Nashville", "Salt Lake City", "Raleigh", "Detroit", "Pittsburgh",
    "Boulder", "Palo Alto", "Washington DC", "Charlotte", "Minneapolis", "Houston",
    "San Diego", "Indianapolis", "Columbus", "Tampa", "Orlando", "Las Vegas",
]


def load_us_cities() -> list[str]:
    if CITIES_PATH.exists():
        cities: list[str] = []
        seen: set[str] = set()
        for line in CITIES_PATH.read_text(encoding="utf-8").splitlines():
            city = line.strip()
            if not city or city.startswith("#"):
                continue
            key = city.lower()
            if key not in seen:
                seen.add(key)
                cities.append(city)
        if cities:
            return cities
    return list(_US_CITIES_FALLBACK)


US_CITIES = load_us_cities()

ALL_PHASES = (
    "fund_flow",
    "http_seeds",
    "ddgs",
    "webclaw",
    "agentcrawl",
    "crawl4ai",
    "classify",
    "company_name",
    # Evidence-only age proxy enrichment (never inferred from name/appearance
    # — see quality.extract_age_proxy). Runs after company_name so the
    # qualify phase below has the best available evidence.
    "age",
    "exa",
    # Generic target-criteria qualification (quality.qualify_row). Should
    # run last, after company_name/age enrichment, so qualification uses the
    # best available evidence for each row.
    "qualify",
)


def load_env() -> None:
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / "scripts" / ".env")
    hermes_env = Path.home() / ".hermes" / ".env"
    if hermes_env.exists():
        load_dotenv(hermes_env)
    local_hermes = Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / ".env"
    if local_hermes.exists():
        load_dotenv(local_hermes)


def build_discovery_queries() -> list[str]:
    """Deprecated: nationwide CPA-partner discovery queries.

    This function now just delegates to the generic, TargetConfig-driven
    query_generator using the CPA preset, so it produces (approximately —
    a handful of hand-written broad phrasings from the old version are not
    reproduced verbatim, but the state/city/title cross-product coverage is
    the same or broader) the same query set the hard-coded CPA version did.
    Kept only so old callers importing this function directly don't break.
    New code should call query_generator.build_queries(your_target_config).
    """
    from .query_generator import build_queries
    from .target_config import CPA_PARTNER_PRESET

    return build_queries(CPA_PARTNER_PRESET)


# The TargetConfig currently driving discovery for this process. Set once by
# the orchestrator at the start of a run via set_active_target(). Defaults to
# the CPA preset so `shuffled_queries()` — called with no arguments by
# sources/ddgs_search.py and sources/exa_search.py — keeps working exactly as
# before for any script that never calls set_active_target at all.
_ACTIVE_TARGET = None  # type: ignore[var-annotated]


def set_active_target(target) -> None:  # target: TargetConfig, avoid import cycle in signature
    """Called once by the orchestrator before running discovery phases."""
    global _ACTIVE_TARGET
    _ACTIVE_TARGET = target


def get_active_target():
    from .target_config import CPA_PARTNER_PRESET

    return _ACTIVE_TARGET if _ACTIVE_TARGET is not None else CPA_PARTNER_PRESET


def shuffled_queries(target=None, seed: int | None = None) -> list[str]:
    """Query list for the active (or given) TargetConfig, shuffled.

    Backward compatible: callers that pass nothing get the currently active
    target (CPA preset by default), exactly matching the old zero-arg call
    sites in sources/ddgs_search.py and sources/exa_search.py.
    """
    from . import config as cfg
    from .query_generator import build_queries

    use_seed = cfg.QUERY_SHUFFLE_SEED if seed is None else seed
    use_target = target if target is not None else get_active_target()
    queries = build_queries(use_target)
    rng = random.Random(use_seed)
    rng.shuffle(queries)
    return queries


@dataclass
class DirectorySeed:
    name: str
    url: str
    engine: str
    max_pages: int = 25
    max_depth: int = 2
    include_patterns: list[str] = field(default_factory=list)


def load_directory_seeds(engine: str | None = None) -> list[DirectorySeed]:
    if not SEEDS_PATH.exists():
        return []
    raw = yaml.safe_load(SEEDS_PATH.read_text(encoding="utf-8")) or {}
    seeds: list[DirectorySeed] = []
    for item in raw.get("seeds", []):
        seed = DirectorySeed(
            name=item["name"],
            url=item["url"],
            engine=item["engine"],
            max_pages=int(item.get("max_pages", 25)),
            max_depth=int(item.get("max_depth", 2)),
            include_patterns=list(item.get("include_patterns") or []),
        )
        if engine is None or seed.engine == engine:
            seeds.append(seed)
    return seeds


def parse_phases(phases_arg: str | None) -> list[str]:
    if not phases_arg:
        return [p for p in ALL_PHASES if p != "exa"]
    phases = [p.strip().lower() for p in phases_arg.split(",") if p.strip()]
    unknown = set(phases) - set(ALL_PHASES)
    if unknown:
        raise ValueError(f"Unknown phases: {', '.join(sorted(unknown))}")
    return phases
