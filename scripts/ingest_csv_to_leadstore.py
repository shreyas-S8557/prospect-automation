"""Bridge script: load a discovery-stage CSV (the output of
scripts/pipeline/orchestrator.py, e.g. data/saas_ai_founders_qualified.csv)
into the LeadStore SQLite database, so the downstream stage CLIs
(email_discovery.py, email_validation.py, email_generation.py,
email_sending.py) can pick the leads up by campaign_id.

This is the missing link between "I have a discovery CSV" and "the
per-stage `python -m pipeline.<stage>` commands documented in
HOW_TO_USE.md operate on LeadStore, not on a CSV directly."

Usage:
    python3 scripts/ingest_csv_to_leadstore.py \
        --csv data/saas_ai_founders_qualified.csv \
        --campaign-id saas_ai_founders \
        --config data/target_configs/saas_founders.json \
        [--db data/pipeline_state.db]

If the CSV already carries a `qualification_status` column (as
orchestrator.py's output does), rows marked "qualified" there are trusted
directly rather than re-run through matches_target_criteria — this keeps
one CSV row's qualification decision consistent with the discovery run
that produced it. Rows with no `qualification_status` column are
qualified fresh against --config (or the CPA preset default, matching
every other zero-arg call site in this project, if --config is omitted).
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline.campaign import UnsupportedTemplateVariable, ensure_campaign, load_campaign  # noqa: E402
from pipeline.config import load_env  # noqa: E402
from pipeline.lead_pipeline import ingest_discovery_rows, qualify_pending_leads  # noqa: E402
from pipeline.lead_store import DEFAULT_DB_PATH, LeadStore  # noqa: E402
from pipeline.models import InvestorRow, PipelineStatus  # noqa: E402
from pipeline.target_config import TargetConfig  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True, help="Discovery-stage CSV to ingest")
    ap.add_argument("--campaign-id", required=True, help="Campaign id leads are grouped under")
    ap.add_argument(
        "--config", default=None,
        help="TargetConfig JSON (only used for rows with no qualification_status column already set)",
    )
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH), help="LeadStore SQLite path")
    args = ap.parse_args()

    load_env()
    target = TargetConfig.from_json_file(args.config) if args.config else None

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"error: {csv_path} not found", file=sys.stderr)
        return 1

    with csv_path.open(encoding="utf-8", newline="") as f:
        rows: list[InvestorRow] = list(csv.DictReader(f))  # type: ignore[assignment]

    if not rows:
        print("error: CSV has no rows", file=sys.stderr)
        return 1

    pre_qualified = [r for r in rows if (r.get("qualification_status") or "").strip()]
    to_qualify_fresh = [r for r in rows if not (r.get("qualification_status") or "").strip()]

    with LeadStore(args.db) as store:
        # Make sure a Campaign row exists for this campaign_id before/alongside
        # ingesting leads, so downstream stages (email_generation,
        # email_sending, campaign stats/control) can always load_campaign()
        # successfully once leads have been ingested here. ensure_campaign()
        # is idempotent -- it never creates a duplicate on re-ingestion, and
        # never overwrites a campaign that already exists (e.g. one someone
        # customized via scripts/create_campaign.py). Campaign copy is
        # sourced from --config's optional email_subject_template /
        # email_body_template fields when present; otherwise a generic,
        # non-vertical-specific placeholder template is used, matching the
        # same fallback create_campaign.py's docs point users at replacing.
        campaign_existed_already = load_campaign(store, args.campaign_id) is not None
        try:
            campaign = ensure_campaign(
                store,
                args.campaign_id,
                name=(target.campaign_name if target else None) or (target.name if target else None),
                description=target.campaign_description if target else "",
                subject_template=target.email_subject_template if target else None,
                body_template=target.email_body_template if target else None,
                sender_name=target.email_sender_name if target else "",
            )
        except UnsupportedTemplateVariable as exc:
            print(f"error: --config email template invalid: {exc}", file=sys.stderr)
            return 1
        print(
            f"Campaign: {campaign.campaign_id!r} ({campaign.name!r}) "
            f"[{'reused existing' if campaign_existed_already else 'created'}]"
        )

        stats = ingest_discovery_rows(store, rows, campaign_id=args.campaign_id)
        print(f"Ingested: created={stats['created']} updated={stats['updated']} no_identity={stats['no_identity']}")

        # Rows the CSV already labelled qualified/disqualified: trust that
        # label directly (transition instead of re-deriving it) so this
        # step can't disagree with the discovery run that produced the CSV.
        if pre_qualified:
            by_key = {}
            for lead in store.list_by_status(PipelineStatus.DISCOVERED, campaign_id=args.campaign_id):
                by_key[lead.identity_key] = lead
            trusted_qualified = trusted_filtered = 0
            for row in pre_qualified:
                from pipeline.lead_pipeline import normalize_investor_row

                probe = normalize_investor_row(row, campaign_id=args.campaign_id)
                lead = by_key.get(probe.identity_key)
                if lead is None:
                    continue
                status = (row.get("qualification_status") or "").strip().lower()
                if status == "qualified":
                    store.transition(lead.lead_id, PipelineStatus.QUALIFIED)
                    trusted_qualified += 1
                else:
                    store.transition(lead.lead_id, PipelineStatus.FILTERED_OUT)
                    trusted_filtered += 1
            print(f"Applied CSV qualification labels: qualified={trusted_qualified} filtered_out={trusted_filtered}")

        # Anything left in DISCOVERED (no qualification_status in the CSV)
        # gets qualified fresh against --config.
        if to_qualify_fresh:
            qstats = qualify_pending_leads(store, campaign_id=args.campaign_id, target=target)
            print(f"Qualified fresh: qualified={qstats['qualified']} filtered_out={qstats['filtered_out']}")

        qualified_n = len(store.list_by_status(PipelineStatus.QUALIFIED, campaign_id=args.campaign_id))
        print(f"\nTotal QUALIFIED leads ready for email discovery: {qualified_n}")
        print(f"LeadStore: {args.db}")
        print(
            "\nNext: python -m pipeline.email_discovery --campaign-id "
            f"{args.campaign_id} --db {args.db}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
