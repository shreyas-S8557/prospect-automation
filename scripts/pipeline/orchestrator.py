"""Phase orchestrator for angel investor collection."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

import httpx
from openai import OpenAI
from tqdm import tqdm

from .config import (
    MEMORY_PATH,
    MEMORY_PATHS,
    OUTPUT,
    OUTPUT_V2,
    QUERY_SHUFFLE_SEED,
    TARGET,
    load_env,
    parse_phases,
    set_active_target,
)
from .target_config import CPA_PARTNER_PRESET, TargetConfig
from .llm import (
    classify_industries_llm,
    extract_ages_llm,
    extract_company_names_llm,
    get_llm_client,
)
from .models import InvestorRow
from .quality import (
    add_investor,
    dedupe_investors,
    dedup_memory_size,
    extract_age_proxy,
    find_age_evidence_via_search,
    linkedin_slug,
    load_dedup_memory,
    load_existing_csv,
    qualify_row,
    save_csv,
)
from .sources.agentcrawl_crawl import run_agentcrawl_phase
from .sources.crawl4ai_crawl import run_crawl4ai_phase
from .sources.ddgs_search import ddgs_search, run_ddgs_phase
from .sources.exa_search import run_exa_phase
from .sources.fund_flow import build_email_lookup, fetch_fund_flow
from .sources.http_seed_fetch import run_http_seed_phase
from .sources.webclaw_crawl import run_webclaw_phase


def _force_utf8_stdio() -> None:
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass


def backup_output(path: Path) -> None:
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = path.with_name(f"{path.stem}_backup_{stamp}{path.suffix}")
        path.rename(backup)
        print(f"Backed up prior sheet -> {backup}", file=sys.stderr)


def resolve_output_path(args: argparse.Namespace, target: TargetConfig) -> Path:
    if getattr(args, "output", None):
        p = Path(args.output)
        return p if p.is_absolute() else (OUTPUT.parent / p)
    if getattr(args, "v2", False):
        return OUTPUT_V2
    if target is CPA_PARTNER_PRESET:
        # Unchanged default path for the legacy CPA run, byte-for-byte the
        # same as before this refactor.
        return OUTPUT
    return OUTPUT.parent / f"{target.output_stem()}.csv"


def build_target_config(args: argparse.Namespace) -> TargetConfig:
    """Build the TargetConfig driving this run, in priority order:

    1. --config <path.json>          (full TargetConfig JSON)
    2. any of --titles/--locations/--industries/--keywords/--age-min/
       --age-max/--company-size-min/--company-size-max/--campaign-name given
       on the CLI (built from individual flags)
    3. neither given -> CPA_PARTNER_PRESET (unchanged legacy behaviour)
    """
    if getattr(args, "config", None):
        target = TargetConfig.from_json_file(args.config)
        # --target on the CLI still overrides target_count even with --config,
        # matching how --target already behaves for the legacy path.
        if args.target != TARGET:
            target.target_count = args.target
        return target

    cli_fields = (
        args.titles, args.locations, args.industries, args.keywords,
        args.age_min, args.age_max, args.company_size_min,
        args.company_size_max, args.campaign_name,
    )
    if any(v not in (None, []) for v in cli_fields):
        return TargetConfig(
            name=args.campaign_name or "campaign",
            locations=args.locations or ["United States"],
            titles=args.titles or [],
            industries=args.industries or [],
            keywords=args.keywords or [],
            age_min=args.age_min,
            age_max=args.age_max,
            company_size_min=args.company_size_min,
            company_size_max=args.company_size_max,
            target_count=args.target,
        )

    return CPA_PARTNER_PRESET


def _needs_llm(phases: list[str], skip_llm_extract: bool) -> bool:
    if "classify" in phases or "company_name" in phases or "age" in phases:
        return True
    if skip_llm_extract:
        return False
    return any(
        p in phases
        for p in ("ddgs", "http_seeds", "webclaw", "agentcrawl", "crawl4ai")
    )


def _get_llm_optional() -> OpenAI | None:
    if not os.getenv("OPENAI_API_KEY"):
        print("WARNING: OPENAI_API_KEY not set; LLM steps skipped.", file=sys.stderr)
        return None
    return get_llm_client()


def run_pipeline(args: argparse.Namespace, *, on_candidate=None, on_query_progress=None) -> int:
    _force_utf8_stdio()
    load_env()

    target_config = build_target_config(args)
    set_active_target(target_config)
    print(
        f"Target config: name={target_config.name!r} "
        f"titles={target_config.titles or '(none)'} "
        f"industries={target_config.industries or '(none)'} "
        f"locations={target_config.locations} "
        f"keywords={target_config.keywords or '(none)'} "
        f"target_count={target_config.target_count}",
        file=sys.stderr,
    )

    output_path = resolve_output_path(args, target_config)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # target_config.target_count is the source of truth (already reconciled
    # with --target inside build_target_config for the --config path too).
    target = target_config.target_count
    phases = parse_phases(args.phases)

    # Isolate query order from v1 runs when writing v2
    if output_path.resolve() != OUTPUT.resolve():
        from . import config as cfg

        cfg.QUERY_SHUFFLE_SEED = QUERY_SHUFFLE_SEED + 17

    llm: OpenAI | None = None
    if _needs_llm(phases, args.skip_llm_extract):
        llm = _get_llm_optional()

    # Always load baseline memory BEFORE --fresh renames the CSV away.
    # Memory is compare-only: blocks re-adding known LinkedIn slugs, not written unless
    # those rows were also loaded into `investors` from this run's output path.
    print("Loading dedup memory...", file=sys.stderr)
    load_dedup_memory(MEMORY_PATHS)

    # --fresh / --v2: do not seed output from sibling CSVs; still blocked by memory above
    if args.fresh or (getattr(args, "v2", False) and not args.resume):
        if args.fresh and output_path.exists():
            backup_output(output_path)
        investors: dict[str, InvestorRow] = {}
        initial_count = 0
    elif args.resume:
        investors = load_existing_csv(output_path)
        initial_count = len(investors)
    else:
        # Default: start empty when targeting a new file that doesn't exist yet
        if output_path.exists():
            investors = load_existing_csv(output_path)
            initial_count = len(investors)
        else:
            investors = {}
            initial_count = 0

    http = httpx.Client(timeout=30, follow_redirects=True)

    def checkpoint() -> None:
        save_csv(output_path, dedupe_investors(investors))

    print(f"Output -> {output_path}", file=sys.stderr)
    print(
        f"Starting collection target={target} | in-memory={len(investors)} "
        f"| dedup-blocked={dedup_memory_size()}",
        file=sys.stderr,
    )
    print(f"Phases: {', '.join(phases)}", file=sys.stderr)

    email_by_name: dict[str, str] = {}

    if "fund_flow" in phases:
        print("Fetching Fund-Flow angel CSV...", file=sys.stderr)
        ff_added = 0
        try:
            for row in fetch_fund_flow(http):
                if add_investor(investors, row):
                    ff_added += 1
            print(f"  +{ff_added} from Fund-Flow (total {len(investors)})", file=sys.stderr)
        except Exception as exc:
            # This source is a single hard-coded external CSV (angel-investor
            # data) that has nothing to do with most campaigns (e.g. a SaaS/
            # AI founders search). A transient failure here (rate limiting,
            # the file moving, a network blip) must never take down the rest
            # of the run — ddgs/classify/company_name/age/qualify all still
            # need to run regardless of whether this one optional source
            # is reachable right now.
            print(f"  WARN: fund_flow source unavailable, skipping ({exc})", file=sys.stderr)
        try:
            email_by_name = build_email_lookup(http)
        except Exception as exc:
            print(f"  WARN: fund_flow email lookup unavailable, skipping ({exc})", file=sys.stderr)

    if "http_seeds" in phases:
        if llm is None and not args.skip_llm_extract:
            llm = _get_llm_optional()
        run_http_seed_phase(
            investors,
            http=http,
            llm=llm,
            target=target,
            skip_llm_extract=args.skip_llm_extract,
        )
        checkpoint()

    if "ddgs" in phases:
        if llm is None and not args.skip_llm_extract:
            llm = _get_llm_optional()
        run_ddgs_phase(
            investors,
            llm=llm,
            target=target,
            skip_llm_extract=args.skip_llm_extract,
            on_checkpoint=checkpoint,
            on_candidate=on_candidate,
            on_query_progress=on_query_progress,
        )
        # Always checkpoint the CSV right after ddgs too (previously this
        # phase only relied on the 75-added-rows threshold inside
        # run_ddgs_phase, which a small target_count like 10 may never
        # reach -- meaning the CSV on disk could stay stale/empty for the
        # entire phase).
        checkpoint()

    if "webclaw" in phases:
        if llm is None and not args.skip_llm_extract:
            llm = _get_llm_optional()
        run_webclaw_phase(
            investors,
            llm=llm,
            target=target,
            skip_llm_extract=args.skip_llm_extract or llm is None,
        )

    if "agentcrawl" in phases:
        if llm is None and not args.skip_llm_extract:
            llm = _get_llm_optional()
        run_agentcrawl_phase(
            investors,
            llm=llm,
            target=target,
            skip_llm_extract=args.skip_llm_extract or llm is None,
        )

    if "crawl4ai" in phases:
        if llm is None and not args.skip_llm_extract:
            llm = _get_llm_optional()
        run_crawl4ai_phase(
            investors,
            llm=llm,
            target=target,
            skip_llm_extract=args.skip_llm_extract or llm is None,
        )
        checkpoint()

    if "exa" in phases or args.use_exa:
        exa_key = os.getenv("EXA_API_KEY", "")
        if not exa_key:
            print("WARN: --use-exa requested but EXA_API_KEY not set", file=sys.stderr)
        else:
            run_exa_phase(
                investors,
                http=http,
                exa_key=exa_key,
                target=target,
                on_checkpoint=checkpoint,
            )

    investors = dedupe_investors(investors)
    rows = sorted(investors.values(), key=lambda r: (r.get("name") or "").lower())
    if args.fresh:
        rows = rows[:target]
    elif target >= initial_count:
        rows = rows[:target]
    # else: resume with target below loaded count — do not shrink existing sheet

    if not email_by_name and rows:
        try:
            email_by_name = build_email_lookup(http)
        except Exception as exc:
            print(f"  WARN: email lookup unavailable, skipping ({exc})", file=sys.stderr)

    for row in rows:
        if not row.get("email"):
            row["email"] = email_by_name.get(row.get("name", "").lower(), "")

    if "classify" in phases:
        if llm is None:
            llm = _get_llm_optional()
        if llm:
            print(f"Classifying industries for {len(rows)} profiles via FreeLLMAPI...", file=sys.stderr)
            batch_size = 20
            for i in tqdm(range(0, len(rows), batch_size), desc="LLM"):
                batch = rows[i : i + batch_size]
                need = [r for r in batch if not r.get("industries")]
                if need:
                    classify_industries_llm(llm, need)

    if "company_name" in phases:
        if llm is None:
            llm = _get_llm_optional()
        if llm:
            print(f"Extracting company names for {len(rows)} profiles via FreeLLMAPI...", file=sys.stderr)
            batch_size = 20
            for i in tqdm(range(0, len(rows), batch_size), desc="Company LLM"):
                batch = rows[i : i + batch_size]
                need = [r for r in batch if not r.get("company_name")]
                if need:
                    extract_company_names_llm(llm, need)
        else:
            for row in rows:
                row.setdefault("company_name", "")

    if "age" in phases:
        print(f"Enriching age proxy for {len(rows)} profiles (evidence-only)...", file=sys.stderr)
        regex_found = 0
        for row in rows:
            has_usable_proxy = row.get("age") and str(row.get("age_confidence", "")).lower() not in ("", "none")
            if has_usable_proxy:
                continue
            age, source, confidence = extract_age_proxy(row)
            if age:
                row["age"], row["age_source"], row["age_confidence"] = age, source, confidence
                regex_found += 1
            else:
                row.setdefault("age", "")
                row.setdefault("age_source", "")
                row["age_confidence"] = row.get("age_confidence") or "none"
        print(f"  age proxy found via explicit text evidence: {regex_found}/{len(rows)}", file=sys.stderr)

        if llm is None:
            llm = _get_llm_optional()
        if llm:
            need = [
                r for r in rows
                if not r.get("age") and str(r.get("age_confidence", "")).lower() in ("", "none")
            ]
            if need:
                print(f"Falling back to LLM age evidence-check for {len(need)} profiles...", file=sys.stderr)
                batch_size = 20
                for i in tqdm(range(0, len(need), batch_size), desc="Age LLM"):
                    extract_ages_llm(llm, need[i : i + batch_size])

        # Web-search enrichment: the regex and LLM passes above only ever
        # look at the discovery-time snippet (profile_title + summary),
        # which essentially never states an age or graduation year — so
        # both are structurally unable to find evidence that was never
        # fetched. This runs 1-2 targeted searches per still-unresolved
        # candidate specifically for graduation-year phrasing, and only
        # ever trusts what comes back through the exact same evidence-only
        # extract_age_proxy() check (see quality.find_age_evidence_via_search).
        # Best-effort: age/graduation-year info is genuinely sparse on the
        # open web for most people, so this often still won't find anything
        # — but unlike the passes above, it's actually looking somewhere new.
        still_need = [
            r for r in rows
            if not r.get("age") and str(r.get("age_confidence", "")).lower() in ("", "none")
        ]
        if still_need:
            try:
                import ddgs as _ddgs_probe  # noqa: F401
            except ImportError:
                print(
                    "  age web-search enrichment skipped (ddgs package not installed)",
                    file=sys.stderr,
                )
            else:
                print(
                    f"Searching the web for age/graduation-year evidence for "
                    f"{len(still_need)} still-unresolved profiles...",
                    file=sys.stderr,
                )
                search_found = 0
                for row in tqdm(still_need, desc="Age web search"):
                    age, source, confidence = find_age_evidence_via_search(
                        row, lambda q, n: ddgs_search(q, n, linkedin_only=False)
                    )
                    if age:
                        row["age"], row["age_source"], row["age_confidence"] = age, source, confidence
                        search_found += 1
                print(f"  age proxy found via web search: {search_found}/{len(still_need)}", file=sys.stderr)

    if "qualify" in phases:
        print(f"Qualifying {len(rows)} profiles against target criteria...", file=sys.stderr)
        qualified_n = 0
        disqualified_n = 0
        for row in rows:
            ok, reason = qualify_row(row, target_config)
            row["qualification_status"] = "qualified" if ok else "disqualified"
            row["qualification_reason"] = reason
            if ok:
                qualified_n += 1
            else:
                disqualified_n += 1
        print(f"  qualified={qualified_n} disqualified={disqualified_n}", file=sys.stderr)

        qualified_path = output_path.with_name(f"{output_path.stem}_qualified{output_path.suffix}")
        qualified_rows = {
            linkedin_slug(r["linkedin_url"]): r
            for r in rows
            if r.get("qualification_status") == "qualified" and linkedin_slug(r.get("linkedin_url", ""))
        }
        save_csv(qualified_path, qualified_rows)
        print(f"  wrote {len(qualified_rows)} qualified leads -> {qualified_path}", file=sys.stderr)

    save_csv(output_path, {linkedin_slug(r["linkedin_url"]): r for r in rows if linkedin_slug(r.get("linkedin_url", ""))})

    slugs = {linkedin_slug(r["linkedin_url"]) for r in rows}
    with_email = sum(1 for r in rows if r.get("email"))
    with_phone = sum(1 for r in rows if r.get("phone"))
    source_counts: dict[str, int] = {}
    for r in rows:
        src = r.get("source", "unknown")
        source_counts[src] = source_counts.get(src, 0) + 1

    print(f"\nSaved {len(rows)} unique investors -> {output_path}", file=sys.stderr)
    print(f"  unique_slugs={len(slugs)} sources={source_counts}", file=sys.stderr)
    print(f"  with_email={with_email} with_phone={with_phone}", file=sys.stderr)
    if len(rows) < target:
        print(f"  Short of target ({len(rows)}/{target})", file=sys.stderr)

    http.close()
    return 0


def run_discovery(
    target: TargetConfig,
    *,
    phases: str | None = None,
    fresh: bool = False,
    resume: bool = False,
    output: str | None = None,
    skip_llm_extract: bool = False,
    on_candidate=None,
    on_query_progress=None,
) -> Path:
    """Programmatic entry point — run discovery for a TargetConfig object
    directly, without going through argparse/the CLI. This is the call the
    application backend (Day 4+) should use instead of shelling out to this
    script as a subprocess.

    Returns the path to the resulting CSV.
    """
    args = argparse.Namespace(
        fresh=fresh,
        resume=resume,
        v2=False,
        output=output,
        target=target.target_count,
        phases=phases,
        skip_llm_extract=skip_llm_extract,
        use_exa=False,
        config=None,
        campaign_name=target.name,
        titles=None,
        locations=None,
        industries=None,
        keywords=None,
        age_min=None,
        age_max=None,
        company_size_min=None,
        company_size_max=None,
    )
    # build_target_config(args) will see config=None and all the individual
    # target fields as None, so it would normally fall through to the CPA
    # preset — that's not what we want here, since the caller already handed
    # us a real TargetConfig. Monkeypatch it in via args.config through a
    # throwaway temp file so run_pipeline's normal path picks it up exactly
    # like a user-supplied --config would, keeping this a single code path
    # instead of a second, divergent one.
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tf:
        target.to_json_file(tf.name)
        args.config = tf.name

    run_pipeline(args, on_candidate=on_candidate, on_query_progress=on_query_progress)
    return resolve_output_path(args, target)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect US angel investors (LinkedIn /in/ discovery + directory harvest)"
    )
    parser.add_argument("--fresh", action="store_true", help="Backup this output CSV and start empty")
    parser.add_argument("--resume", action="store_true", help="Resume from this output CSV only")
    parser.add_argument(
        "--v2",
        action="store_true",
        help=(
            f"Write to {OUTPUT_V2.name} (isolated output; still dedups against "
            f"{MEMORY_PATH.name})"
        ),
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Custom output CSV path (overrides --v2 / default)",
    )
    parser.add_argument("--target", type=int, default=TARGET)
    parser.add_argument(
        "--phases",
        type=str,
        default=None,
        help=(
            "Comma-separated: fund_flow,http_seeds,ddgs,webclaw,agentcrawl,crawl4ai,"
            "classify,company_name,age,exa,qualify"
        ),
    )
    parser.add_argument(
        "--skip-llm-extract",
        action="store_true",
        help="Heuristic-only parsing; skip LLM extract/parse fallbacks",
    )
    parser.add_argument(
        "--use-exa",
        action="store_true",
        help="Legacy: also run Exa people search if EXA_API_KEY is set",
    )

    target_group = parser.add_argument_group(
        "Target criteria",
        description=(
            "Configure who to discover, without editing source code. "
            "Give either --config (a full TargetConfig JSON file) or any "
            "combination of the individual flags below. If none of these "
            "are given, defaults to the legacy CPA-partner criteria."
        ),
    )
    target_group.add_argument(
        "--config", type=str, default=None,
        help="Path to a TargetConfig JSON file (see data/target_configs/example.json)",
    )
    target_group.add_argument(
        "--campaign-name", type=str, default=None,
        help="Label for this campaign; also used to name the output CSV",
    )
    target_group.add_argument(
        "--titles", type=str, nargs="*", default=None,
        help="Job title(s) to search for, e.g. --titles Founder CEO CTO",
    )
    target_group.add_argument(
        "--locations", type=str, nargs="*", default=None,
        help="Target location(s), e.g. --locations \"United States\"",
    )
    target_group.add_argument(
        "--industries", type=str, nargs="*", default=None,
        help="Target industries, e.g. --industries SaaS Fintech",
    )
    target_group.add_argument(
        "--keywords", type=str, nargs="*", default=None,
        help="Free-text keywords, e.g. --keywords AI Automation",
    )
    target_group.add_argument("--age-min", type=int, default=None)
    target_group.add_argument("--age-max", type=int, default=None)
    target_group.add_argument("--company-size-min", type=int, default=None)
    target_group.add_argument("--company-size-max", type=int, default=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint: `python -m scripts.pipeline.orchestrator [args]`.

    This was previously missing — build_arg_parser()/run_pipeline() existed
    but nothing ever called them when the module was run directly. Added so
    the orchestrator is actually invocable as documented.
    """
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return run_pipeline(args)


if __name__ == "__main__":
    sys.exit(main())
