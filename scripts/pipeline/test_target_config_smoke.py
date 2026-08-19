"""Day 2 milestone smoke test: prove discovery criteria are now configurable.

Run with:
    python -m scripts.pipeline.test_target_config_smoke

This does NOT hit the network or an LLM — it only proves:
  1. TargetConfig can be built from a plain dict / JSON file (the shape the
     future UI form and API will send).
  2. Two different TargetConfigs produce two different, non-CPA-flavoured
     query sets — i.e. the query generator is actually driven by the config,
     not by hard-coded CPA constants.
  3. The old zero-arg call sites (shuffled_queries(), as called by
     sources/ddgs_search.py and sources/exa_search.py) still work and still
     default to CPA behaviour when nothing else is configured — proving we
     didn't break the existing working scraper.
  4. Output file naming derives from the campaign, so two campaigns don't
     collide on one CSV (no manual handoff needed between runs).
"""

from __future__ import annotations

import sys

from . import config
from .orchestrator import build_arg_parser, build_target_config, resolve_output_path
from .target_config import CPA_PARTNER_PRESET, TargetConfig
from .query_generator import build_queries


def check(label: str, condition: bool) -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        sys.exit(1)


def main() -> None:
    # --- 1. TargetConfig from a plain dict (what the UI form / API sends) ---
    saas_config_dict = {
        "locations": ["United States"],
        "titles": ["Founder", "CEO", "CTO"],
        "industries": ["SaaS"],
        "keywords": ["AI", "automation"],
        "target_count": 1000,
        "age_min": 22,
        "age_max": 35,
        "name": "saas_ai_founders",
    }
    saas_target = TargetConfig.from_dict(saas_config_dict)
    check("TargetConfig builds from a plain dict", saas_target.titles == ["Founder", "CEO", "CTO"])

    # --- 2. Round-trip through JSON (the --config file path) ---
    tmp_path = "/tmp/_day2_smoke_target.json"
    saas_target.to_json_file(tmp_path)
    reloaded = TargetConfig.from_json_file(tmp_path)
    check("TargetConfig round-trips through JSON", reloaded == saas_target)

    # --- 3. Query generator is actually driven by the config, not hard-coded ---
    saas_queries = build_queries(saas_target)
    check("SaaS config produces queries", len(saas_queries) > 0)
    check(
        "SaaS queries mention the configured title 'Founder'",
        any("founder" in q.lower() for q in saas_queries),
    )
    check(
        "SaaS queries do NOT contain hard-coded CPA language",
        not any("cpa" in q.lower() or "accounting" in q.lower() for q in saas_queries),
    )

    cpa_queries = build_queries(CPA_PARTNER_PRESET)
    check(
        "CPA preset still produces CPA-flavoured queries (legacy behaviour preserved)",
        any("cpa" in q.lower() or "accounting" in q.lower() for q in cpa_queries),
    )
    check(
        "Two different configs produce two different query sets",
        set(saas_queries) != set(cpa_queries),
    )

    # --- 4. Backward compatibility: shuffled_queries() with zero args ---
    # (this is exactly how sources/ddgs_search.py and sources/exa_search.py
    # call it — must keep defaulting to CPA behaviour when nothing is set)
    config._ACTIVE_TARGET = None  # simulate a fresh process, nothing set yet
    default_queries = config.shuffled_queries()
    check(
        "shuffled_queries() with no active target defaults to CPA behaviour",
        any("cpa" in q.lower() for q in default_queries),
    )

    config.set_active_target(saas_target)
    active_queries = config.shuffled_queries()
    check(
        "shuffled_queries() with no args uses the active target once set",
        any("founder" in q.lower() for q in active_queries)
        and not any("cpa" in q.lower() for q in active_queries),
    )
    config._ACTIVE_TARGET = None  # reset for cleanliness

    # --- 5. CLI wiring: --titles/--locations/... builds the same kind of config ---
    parser = build_arg_parser()
    args = parser.parse_args(
        [
            "--titles", "Founder", "CEO", "CTO",
            "--industries", "SaaS",
            "--keywords", "AI", "automation",
            "--target", "1000",
            "--campaign-name", "saas_ai_founders",
        ]
    )
    cli_target = build_target_config(args)
    check("CLI flags build a TargetConfig", cli_target.titles == ["Founder", "CEO", "CTO"])
    check("CLI flags set target_count from --target", cli_target.target_count == 1000)

    # --- 6. Legacy CLI invocation (nothing passed) still resolves to CPA preset ---
    legacy_args = parser.parse_args([])
    legacy_target = build_target_config(legacy_args)
    check(
        "No CLI flags at all -> legacy CPA preset (unchanged default behaviour)",
        legacy_target is CPA_PARTNER_PRESET,
    )
    check(
        "Legacy output path is unchanged from before this refactor",
        resolve_output_path(legacy_args, legacy_target).name == "us_cpa_partners_1000.csv",
    )

    # --- 7. Different campaigns write to different files automatically ---
    saas_output = resolve_output_path(args, cli_target)
    check(
        "A named campaign gets its own output filename",
        saas_output.name == "saas_ai_founders.csv",
    )
    check(
        "SaaS campaign output does not collide with the CPA output file",
        saas_output.name != resolve_output_path(legacy_args, legacy_target).name,
    )

    # --- 8. Config validation actually rejects bad input ---
    try:
        TargetConfig(age_min=40, age_max=20)
        check("Invalid age range is rejected", False)
    except ValueError:
        check("Invalid age range is rejected", True)

    try:
        TargetConfig(target_count=0)
        check("Zero target_count is rejected", False)
    except ValueError:
        check("Zero target_count is rejected", True)

    print("\nAll Day 2 checks passed.")


if __name__ == "__main__":
    main()
