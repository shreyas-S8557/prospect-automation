"""Create (or update) a Campaign — the subject/body template pair that
email_generation.py renders for each EMAIL_VALIDATED lead. No CLI existed
for this before (campaign.py's create_campaign()/save_campaign() are plain
Python functions only) — this fills that gap.

IMPORTANT: --campaign-id here must match the --campaign-id you've been
using for discovery/ingest/email_discovery/email_validation (e.g.
"saas_ai_founders") — Campaign records and Lead records are only linked by
that string, and create_campaign() defaults to a random id if you don't
pass one explicitly.

Supported {{variable}} placeholders in --subject/--body-file: first_name,
last_name, company_name, job_title, location, industry.

Usage:
    python3 scripts/create_campaign.py \
        --campaign-id saas_ai_founders \
        --name "SaaS Founders Outreach" \
        --subject "Quick question about {{company_name}}" \
        --body-file email_body_template.txt \
        --sender-name "Your Name"
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline.campaign import UnsupportedTemplateVariable, create_campaign, save_campaign  # noqa: E402
from pipeline.lead_store import DEFAULT_DB_PATH, LeadStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--campaign-id", required=True, help="Must match the id used for discovery/ingest")
    ap.add_argument("--name", required=True)
    ap.add_argument("--subject", required=True, help="Subject line template, may use {{variables}}")
    body_group = ap.add_mutually_exclusive_group(required=True)
    body_group.add_argument("--body", help="Body template text inline")
    body_group.add_argument("--body-file", help="Path to a file containing the body template")
    ap.add_argument("--description", default="")
    ap.add_argument("--sender-name", default="")
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = ap.parse_args()

    body = args.body
    if args.body_file:
        path = Path(args.body_file)
        if not path.exists():
            print(f"error: {path} not found", file=sys.stderr)
            return 1
        body = path.read_text(encoding="utf-8")

    try:
        campaign = create_campaign(
            args.name,
            args.subject,
            body,
            description=args.description,
            sender_name=args.sender_name,
            campaign_id=args.campaign_id,
        )
    except (ValueError, UnsupportedTemplateVariable) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    with LeadStore(args.db) as store:
        save_campaign(store, campaign)

    print(f"Saved campaign {campaign.campaign_id!r} ({campaign.name!r}) -> {args.db}")
    print(f"Variables used: {sorted(campaign.variables_used) or '(none)'}")
    print(
        "\nNext: python -m pipeline.email_generation --campaign-id "
        f"{campaign.campaign_id} --db {args.db}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
