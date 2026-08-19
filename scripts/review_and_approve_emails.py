"""Review generated email drafts for a campaign, then approve them so
email_sending.py is allowed to send them (leads must be APPROVED first —
see models.PipelineStatus).

No bulk-approve CLI existed for this stage before (email_generation.py's
bulk_approve() is a plain Python function with no command-line wrapper) —
this fills that gap.

Usage:
    # Preview every pending draft for a campaign (does not approve anything):
    python3 scripts/review_and_approve_emails.py --campaign-id saas_ai_founders --db data/pipeline_state.db --preview-only

    # Preview, then approve every pending draft:
    python3 scripts/review_and_approve_emails.py --campaign-id saas_ai_founders --db data/pipeline_state.db --approve-all

    # Approve without printing every draft (e.g. once you've already reviewed via --preview-only):
    python3 scripts/review_and_approve_emails.py --campaign-id saas_ai_founders --db data/pipeline_state.db --approve-all --quiet
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from pipeline.email_generation import REVIEW_PENDING, bulk_approve, list_email_jobs  # noqa: E402
from pipeline.lead_store import DEFAULT_DB_PATH, LeadStore  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--campaign-id", required=True)
    ap.add_argument("--db", default=str(DEFAULT_DB_PATH))
    ap.add_argument("--preview-only", action="store_true", help="Print drafts, approve nothing")
    ap.add_argument("--approve-all", action="store_true", help="Approve every PENDING draft shown")
    ap.add_argument("--quiet", action="store_true", help="Skip printing drafts, just approve")
    args = ap.parse_args()

    if not args.preview_only and not args.approve_all:
        print("error: pass --preview-only, --approve-all, or both", file=sys.stderr)
        return 1

    with LeadStore(args.db) as store:
        jobs = list_email_jobs(store, campaign_id=args.campaign_id, review_status=REVIEW_PENDING)
        if not jobs:
            print("No PENDING drafts found for this campaign.")
            return 0

        if not args.quiet:
            for job in jobs:
                print("=" * 70)
                print(f"lead_id: {job.lead_id}")
                print(f"Subject: {job.subject}")
                print()
                print(job.body)
                print()

        print(f"{len(jobs)} PENDING draft(s) for campaign {args.campaign_id!r}.")

        if args.approve_all:
            result = bulk_approve(store, [j.lead_id for j in jobs])
            print(f"Approved: {len(result.get('approved', []))}")
            if result.get("failed"):
                print(f"Failed:   {len(result['failed'])}")
                for item in result["failed"]:
                    print(f"  {item['lead_id']}: {item['error']}")
            print(
                "\nNext: python -m pipeline.email_sending --campaign-id "
                f"{args.campaign_id} --db {args.db}"
            )

    return 0


if __name__ == "__main__":
    sys.exit(main())
