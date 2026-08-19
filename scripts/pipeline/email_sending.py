"""Day 8: APPROVED EmailJobs -> Email Queue -> Gmail Sender -> SENT / SEND_FAILED.

    APPROVED lead + persisted (Day 7) EmailJob
        -> queue_approved_email(): create a persisted EmailSend row
           (send_status=QUEUED), then transition the Lead APPROVED -> QUEUED
        -> send_queued_email(): transition QUEUED -> SENDING *before* the
           network call, send via GmailSender, then record SENT (with the
           adapter's message_id) or -- after retrying transient failures up
           to max_retries -- SEND_FAILED
        -> queue_pending_approvals() / send_pending_queue(): resumable bulk
           runners, mirroring every earlier stage's *_pending_* helpers

This module is the only place PipelineStatus.QUEUED, SENDING, SENT, and
SEND_FAILED are ever written, mirroring the ownership convention set by
email_discovery.py / email_validation.py / email_generation.py. Nothing in
models.py's Lead/PipelineStatus, lead_pipeline.py, email_discovery.py,
email_validation.py, campaign.py, or email_generation.py is changed by this
module (lead_store.py gained one new, generic, additive `email_sends` table
-- see its "Day 8" section -- for the same reason it already has
`email_candidates`/`campaigns`/`email_jobs`: a persistence home without an
import cycle).

Retry model, and why it doesn't touch the Lead state machine: the existing
ALLOWED_TRANSITIONS treats SENDING as a one-way step into a terminal state
(SENT or SEND_FAILED) -- there's no edge back to QUEUED, matching the
diagram this was built from ("SENDING -> SEND_FAILED", no loop drawn back
to QUEUED). So "retry count" here means retrying the *send attempt itself*
(with a short backoff) inside a single send_queued_email() call, up to
max_retries -- not cycling the Lead's pipeline_status backwards. Only after
every attempt fails does the Lead move to the terminal SEND_FAILED state.

Duplicate-send prevention / resumability: queue_pending_approvals() only
ever pulls leads currently APPROVED, and send_pending_queue() only ever
pulls EmailSend rows currently QUEUED, so a lead already QUEUED, SENDING,
SENT, or SEND_FAILED is never re-selected by either bulk runner on a
restart. UNIQUE(lead_id) on the email_sends table is a second, storage-level
guard against the same lead ever getting two send-queue rows.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from .campaign import Campaign
from .campaign_control import effective_send_settings, get_run_state, RUN_STATE_RUNNING
from .email_generation import EmailJob, REVIEW_APPROVED
from .gmail_sender import GmailSender
from .lead_store import LeadStore
from .models import Lead, PipelineStatus, utc_now_iso, validate_transition
from .suppression import already_contacted, is_suppressed

# send_status values stored on EmailSend rows. Free-text, mirroring the
# review_status convention on EmailJob (Day 7) -- PipelineStatus is the
# state machine; send_status is a queryable mirror of "where is this send"
# that lets list_email_sends() filter without joining against leads. Values
# intentionally match the corresponding PipelineStatus names.
SEND_QUEUED = "QUEUED"
SEND_SENDING = "SENDING"
SEND_SENT = "SENT"
SEND_FAILED = "SEND_FAILED"
SEND_CANCELLED = "CANCELLED"

DEFAULT_MAX_RETRIES = 2
# Conservative, configurable sending controls (item 10: no aggressive
# sending, nothing designed to bypass Gmail's own limits/anti-abuse
# systems). These are defaults, not hard caps -- callers can override them
# per-call, but nothing in this module raises them automatically or removes
# the delay.
DEFAULT_MAX_PER_RUN = 50
DEFAULT_DELAY_SECONDS = 2.0
DEFAULT_RETRY_BACKOFF_SECONDS = 3.0

EMAIL_SEND_FIELDNAMES = [
    "job_id",
    "lead_id",
    "campaign_id",
    "to_email",
    "send_status",
    "retry_count",
    "max_retries",
    "provider_message_id",
    "last_error",
    "queued_at",
    "sending_started_at",
    "sent_at",
    "failed_at",
    "created_at",
    "updated_at",
]


class NoApprovedEmailJob(ValueError):
    """Raised when queueing a lead that has no APPROVED EmailJob for it."""


class NotQueued(ValueError):
    """Raised by send_queued_email if the lead has no QUEUED EmailSend row."""


class SuppressedRecipient(ValueError):
    """Raised by queue_approved_email when the lead's email is on the
    do-not-contact list (see suppression.py). Caught by
    queue_pending_approvals and counted as skipped, never as a hard error --
    one suppressed lead must not abort a batch."""


class DuplicateSendBlocked(ValueError):
    """Raised by queue_approved_email when the lead's email address has
    already been queued/sent/is-sending under a *different* lead_id (see
    suppression.already_contacted) -- item 6/7's duplicate-send protection.
    Pass allow_duplicate=True to queue_approved_email to explicitly
    override this for a deliberate, known re-send."""


@dataclass
class EmailSend:
    """The persisted sending-lifecycle record for one lead's EmailJob."""

    job_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    lead_id: str = ""
    campaign_id: str = ""
    to_email: str = ""
    send_status: str = SEND_QUEUED
    retry_count: int = 0
    max_retries: int = DEFAULT_MAX_RETRIES
    provider_message_id: str = ""
    last_error: str = ""
    queued_at: str = ""
    sending_started_at: str = ""
    sent_at: str = ""
    failed_at: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EmailSend":
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in known and v is not None}
        for int_field in ("retry_count", "max_retries"):
            if int_field in clean:
                clean[int_field] = int(clean[int_field])
        return cls(**clean)


def get_email_send(store: LeadStore, lead_id: str) -> EmailSend | None:
    row = store.get_email_send(lead_id)
    return EmailSend.from_dict(row) if row else None


def list_email_sends(
    store: LeadStore, *, campaign_id: str | None = None, send_status: str | None = None
) -> list[EmailSend]:
    return [
        EmailSend.from_dict(row)
        for row in store.list_email_sends(campaign_id=campaign_id, send_status=send_status)
    ]


# ---------------------------------------------------------------------------
# Queueing: APPROVED -> QUEUED
# ---------------------------------------------------------------------------


def queue_approved_email(
    store: LeadStore,
    lead: Lead,
    *,
    max_retries: int = DEFAULT_MAX_RETRIES,
    allow_duplicate: bool = False,
) -> Lead:
    """Move one APPROVED lead's EmailJob into the send queue.

    Raises InvalidStateTransition (from models.py) if `lead` is not
    currently APPROVED -- queueing is only ever legal from that state,
    checked before any write, mirroring every earlier stage's ordering.

    Raises NoApprovedEmailJob if there's no persisted EmailJob for this
    lead, or its review_status isn't APPROVED (defensive: approve_email()
    in email_generation.py always sets both together, so this should not
    happen via the normal review flow).

    Raises SuppressedRecipient (Day 9, item 7) if the lead's email is on
    the do-not-contact list, and DuplicateSendBlocked (Day 9, item 6) if
    the same address already has an in-flight/completed send under a
    *different* lead_id -- pass allow_duplicate=True to deliberately
    override the latter.

    Idempotent against a lead that already has an EmailSend row (e.g. a
    crash between creating the row and transitioning the Lead): returns
    the lead unchanged rather than raising or duplicating the row. This,
    plus queue_pending_approvals() only ever selecting still-APPROVED
    leads, is what makes queueing safe to re-run after a restart.
    """
    # Check the Lead's own state-machine legality first (and let
    # InvalidStateTransition propagate un-wrapped) so a lead in the wrong
    # pipeline stage always fails the same way regardless of what else is
    # or isn't persisted for it yet.
    validate_transition(lead.pipeline_status, PipelineStatus.QUEUED)

    job_row = store.get_email_job(lead.lead_id)
    if job_row is None or job_row.get("review_status") != REVIEW_APPROVED:
        raise NoApprovedEmailJob(
            f"No APPROVED EmailJob for lead_id={lead.lead_id!r} -- cannot queue for sending"
        )
    if not lead.email.strip():
        raise NoApprovedEmailJob(
            f"Lead lead_id={lead.lead_id!r} has no email address -- cannot queue for sending"
        )

    existing = store.get_email_send(lead.lead_id)
    if existing is not None:
        # Already queued (or further along) -- nothing to do. Covers the
        # crash-between-insert-and-transition race described above.
        return lead

    if is_suppressed(store, lead.email, campaign_id=lead.campaign_id):
        raise SuppressedRecipient(
            f"lead_id={lead.lead_id!r} email={lead.email!r} is on the do-not-contact list"
        )
    if not allow_duplicate and already_contacted(
        store, lead.email, exclude_lead_id=lead.lead_id
    ):
        raise DuplicateSendBlocked(
            f"lead_id={lead.lead_id!r} email={lead.email!r} was already queued/sent "
            "under a different lead_id -- pass allow_duplicate=True to override"
        )

    job = EmailJob.from_dict(job_row)
    now = utc_now_iso()
    send = EmailSend(
        job_id=job.job_id,
        lead_id=lead.lead_id,
        campaign_id=job.campaign_id,
        to_email=lead.email,
        send_status=SEND_QUEUED,
        retry_count=0,
        max_retries=max_retries,
        queued_at=now,
        created_at=now,
        updated_at=now,
    )
    store.save_email_send(send.to_dict())
    return store.transition(lead.lead_id, PipelineStatus.QUEUED)


def queue_pending_approvals(
    store: LeadStore,
    *,
    campaign_id: str | None = None,
    max_retries: int | None = None,
) -> dict[str, int]:
    """Queue every Lead currently APPROVED for `campaign_id` (or all
    campaigns if None).

    Resumable by construction: only APPROVED leads are pulled, so leads
    already moved on to QUEUED/SENDING/SENT/SEND_FAILED are never
    reprocessed on a second run. A lead missing an approved EmailJob, an
    email address, suppressed (item 7), or already contacted under another
    lead_id (item 6) is skipped (counted, not raised) so one bad record
    can't abort the whole batch.

    Day 9 pause/stop (items 1-2): checks the campaign's run-state before
    each lead and stops early (without raising) the moment it's no longer
    RUNNING -- a pause/stop issued mid-run takes effect on the very next
    lead, not just before the next call. Remaining APPROVED leads are left
    untouched and will be picked up by a later call once resumed.

    max_retries=None (the default) resolves through
    campaign_control.effective_send_settings() -- an explicit per-campaign
    override if one was configured via configure_sending(), else
    DEFAULT_MAX_RETRIES -- exactly like send_pending_queue does for its
    settings.
    """
    _, _, resolved_max_retries, _ = effective_send_settings(
        store,
        campaign_id,
        max_per_run=None,
        delay_seconds=None,
        max_retries=max_retries,
        retry_backoff_seconds=None,
        default_max_per_run=DEFAULT_MAX_PER_RUN,
        default_delay_seconds=DEFAULT_DELAY_SECONDS,
        default_max_retries=DEFAULT_MAX_RETRIES,
        default_retry_backoff_seconds=DEFAULT_RETRY_BACKOFF_SECONDS,
    )

    queued = 0
    skipped = 0
    for lead in store.list_by_status(PipelineStatus.APPROVED, campaign_id=campaign_id):
        if campaign_id and get_run_state(store, campaign_id) != RUN_STATE_RUNNING:
            break
        try:
            queue_approved_email(store, lead, max_retries=resolved_max_retries)
            queued += 1
        except (NoApprovedEmailJob, SuppressedRecipient, DuplicateSendBlocked):
            skipped += 1
    return {"queued": queued, "skipped": skipped}


# ---------------------------------------------------------------------------
# Sending: QUEUED -> SENDING -> SENT / SEND_FAILED
# ---------------------------------------------------------------------------


def send_queued_email(
    store: LeadStore,
    gmail: GmailSender,
    lead_id: str,
    *,
    campaign: Campaign | None = None,
    retry_backoff_seconds: float = DEFAULT_RETRY_BACKOFF_SECONDS,
    sleep: Any = time.sleep,
) -> Lead:
    """Send one lead's queued email.

    QUEUED -> SENDING is written *before* the network call, so a crash
    mid-send leaves the record visibly SENDING (inspectable, and never
    silently re-picked-up by send_pending_queue(), which only selects
    QUEUED rows) rather than either losing the attempt or risking a
    duplicate resend on restart.

    On failure, retries the send itself (not the pipeline stage) up to
    `EmailSend.max_retries` additional times with a short backoff, since
    the Lead state machine has no edge back from SENDING to QUEUED (see
    module docstring). Only once every attempt has failed does the Lead
    move to the terminal SEND_FAILED state.

    Raises NotQueued if there's no QUEUED EmailSend row for this lead.
    """
    row = store.get_email_send(lead_id)
    if row is None or row.get("send_status") != SEND_QUEUED:
        raise NotQueued(f"No QUEUED EmailSend for lead_id={lead_id!r}")
    send = EmailSend.from_dict(row)

    job_row = store.get_email_job(lead_id)
    job = EmailJob.from_dict(job_row) if job_row else None
    if job is None:
        # Shouldn't happen (queueing requires an EmailJob) -- fail closed.
        send.send_status = SEND_FAILED
        send.last_error = "EmailJob missing at send time"
        send.failed_at = utc_now_iso()
        send.updated_at = send.failed_at
        store.save_email_send(send.to_dict())
        return store.transition(lead_id, PipelineStatus.SEND_FAILED)

    now = utc_now_iso()
    send.send_status = SEND_SENDING
    send.sending_started_at = now
    send.updated_at = now
    store.save_email_send(send.to_dict())
    store.transition(lead_id, PipelineStatus.SENDING)

    from_name = campaign.sender_name if campaign is not None else ""

    result = None
    attempts = send.max_retries + 1
    for attempt in range(attempts):
        result = gmail.send(send.to_email, job.subject, job.body, from_name=from_name)
        if result.success:
            break
        send.retry_count = attempt + 1
        send.last_error = result.error
        send.updated_at = utc_now_iso()
        store.save_email_send(send.to_dict())
        if attempt < attempts - 1:
            sleep(retry_backoff_seconds)

    now = utc_now_iso()
    if result is not None and result.success:
        send.send_status = SEND_SENT
        send.provider_message_id = result.message_id
        send.last_error = ""
        send.sent_at = now
        send.updated_at = now
        store.save_email_send(send.to_dict())
        return store.transition(lead_id, PipelineStatus.SENT)

    send.send_status = SEND_FAILED
    send.failed_at = now
    send.updated_at = now
    store.save_email_send(send.to_dict())
    return store.transition(lead_id, PipelineStatus.SEND_FAILED)


def send_pending_queue(
    store: LeadStore,
    gmail: GmailSender,
    *,
    campaign: Campaign | None = None,
    campaign_id: str | None = None,
    max_per_run: int | None = None,
    delay_seconds: float | None = None,
    retry_backoff_seconds: float | None = None,
    sleep: Any = time.sleep,
) -> dict[str, int]:
    """Send every currently-QUEUED email, up to `max_per_run`, pausing
    `delay_seconds` between sends.

    Conservative by default (items 3-4: configurable per-run limit and
    inter-send delay): a modest per-run cap and an inter-send delay, both
    configurable but neither removable by accident -- there's no "fast
    mode" flag. Resumable by construction: only rows still QUEUED are
    selected, so leads already SENDING/SENT/SEND_FAILED from a prior
    (possibly crashed) run are never re-sent.

    max_per_run/delay_seconds/retry_backoff_seconds=None (the default)
    resolve through campaign_control.effective_send_settings(): an explicit
    argument here always wins, then a per-campaign override configured via
    configure_sending(), then the module DEFAULT_* constants -- so calling
    this with no arguments behaves exactly as it did in Day 8.

    Day 9 pause/stop (items 1-2): checks the campaign's run-state before
    each send and stops early -- without raising -- the moment it's no
    longer RUNNING, so a pause/stop issued mid-run takes effect before the
    next send goes out, not just before the batch starts. Remaining QUEUED
    rows are left untouched (still QUEUED) for a later resumed run, except
    when the campaign was STOPPED, in which case stop_campaign() itself is
    what already moved those rows to CANCELLED.
    """
    effective_campaign_id = campaign_id or (campaign.campaign_id if campaign else None)
    resolved_max_per_run, resolved_delay, _, resolved_backoff = effective_send_settings(
        store,
        effective_campaign_id,
        max_per_run=max_per_run,
        delay_seconds=delay_seconds,
        max_retries=None,
        retry_backoff_seconds=retry_backoff_seconds,
        default_max_per_run=DEFAULT_MAX_PER_RUN,
        default_delay_seconds=DEFAULT_DELAY_SECONDS,
        default_max_retries=DEFAULT_MAX_RETRIES,
        default_retry_backoff_seconds=DEFAULT_RETRY_BACKOFF_SECONDS,
    )

    queued = store.list_email_sends(
        campaign_id=effective_campaign_id, send_status=SEND_QUEUED, limit=resolved_max_per_run
    )
    sent = 0
    failed = 0
    halted = False
    for i, row in enumerate(queued):
        if effective_campaign_id and get_run_state(store, effective_campaign_id) != RUN_STATE_RUNNING:
            halted = True
            break
        lead = send_queued_email(
            store,
            gmail,
            row["lead_id"],
            campaign=campaign,
            retry_backoff_seconds=resolved_backoff,
            sleep=sleep,
        )
        if lead.status == PipelineStatus.SENT:
            sent += 1
        else:
            failed += 1
        if i < len(queued) - 1:
            sleep(resolved_delay)
    return {"sent": sent, "failed": failed, "halted": halted}


# ---------------------------------------------------------------------------
# Crash recovery: leads stuck in SENDING from a prior run
# ---------------------------------------------------------------------------


def list_stuck_sending(store: LeadStore, *, campaign_id: str | None = None) -> list[EmailSend]:
    """EmailSend rows still SENDING -- i.e. the process was killed after
    marking SENDING but before recording SENT or SEND_FAILED.

    Deliberately not auto-resent: the actual Gmail send call may or may not
    have gone out before the crash, and item 10 (conservative sending,
    nothing that risks duplicate/abusive sends) rules out silently
    resending something that might already have reached the recipient.
    Surface these for a human to check (e.g. against the Gmail "Sent"
    folder) and then explicitly resolve with mark_stuck_as_failed(), which
    re-opens the lead for a deliberate, explicit requeue.
    """
    return list_email_sends(store, campaign_id=campaign_id, send_status=SEND_SENDING)


def mark_stuck_as_failed(store: LeadStore, lead_id: str, *, note: str = "") -> Lead:
    """Explicitly resolve a stuck-SENDING row as SEND_FAILED after manual
    review, so it's out of the ambiguous state. Never called automatically.

    Use this when review determines the send did NOT go out (or couldn't be
    confirmed) -- the conservative default. If review instead *confirms*
    the send did go out (e.g. found in Gmail's Sent folder, or a provider
    message id turned up), use resolve_stuck_as_sent() instead so the lead
    isn't silently re-queued and double-emailed by a well-meaning retry.
    """
    row = store.get_email_send(lead_id)
    if row is None or row.get("send_status") != SEND_SENDING:
        raise NotQueued(f"No SENDING EmailSend for lead_id={lead_id!r}")
    send = EmailSend.from_dict(row)
    now = utc_now_iso()
    send.send_status = SEND_FAILED
    send.last_error = note or "Marked failed after crash recovery review"
    send.failed_at = now
    send.updated_at = now
    store.save_email_send(send.to_dict())
    return store.transition(lead_id, PipelineStatus.SEND_FAILED)


def resolve_stuck_as_sent(
    store: LeadStore, lead_id: str, *, provider_message_id: str = "", note: str = ""
) -> Lead:
    """Explicitly resolve a stuck-SENDING row as SENT, for the one case the
    module docstring's "never auto-resend" rule carves out: a human (or an
    external check against Gmail's own Sent folder/API) has *reliably
    confirmed* the send did in fact go out before the crash. Never called
    automatically -- there is no code path anywhere in this module that
    calls this on its own; a caller must supply the confirmation.
    """
    row = store.get_email_send(lead_id)
    if row is None or row.get("send_status") != SEND_SENDING:
        raise NotQueued(f"No SENDING EmailSend for lead_id={lead_id!r}")
    send = EmailSend.from_dict(row)
    now = utc_now_iso()
    send.send_status = SEND_SENT
    send.provider_message_id = provider_message_id or send.provider_message_id
    send.last_error = note or "Confirmed sent after crash recovery review"
    send.sent_at = now
    send.updated_at = now
    store.save_email_send(send.to_dict())
    return store.transition(lead_id, PipelineStatus.SENT)


if __name__ == "__main__":
    import argparse
    import logging

    from .campaign import load_campaign
    from .campaign_control import pause_campaign, resume_campaign, stop_campaign
    from .campaign_stats import format_campaign_stats, get_campaign_stats
    from .config import load_env

    log = logging.getLogger("pipeline.email_sending")

    ap = argparse.ArgumentParser(
        description="Day 8/9: queue APPROVED emails and send them via Gmail, "
        "with pause/resume/stop and progress-reporting controls."
    )
    ap.add_argument("--db", default=None, help="Path to the LeadStore SQLite file")
    ap.add_argument("--campaign-id", default=None, help="Only process leads for this campaign_id")
    ap.add_argument("--max-per-run", type=int, default=None)
    ap.add_argument("--delay-seconds", type=float, default=None)
    ap.add_argument(
        "--test-email",
        metavar="ADDRESS",
        default=None,
        help="Send a single test email to ADDRESS (or the configured account "
        "if no ADDRESS given) and exit, without touching the queue.",
    )
    ap.add_argument("--pause", action="store_true", help="Pause --campaign-id and exit")
    ap.add_argument("--resume", action="store_true", help="Resume --campaign-id and exit")
    ap.add_argument(
        "--stop", action="store_true", help="Stop (cancel) --campaign-id and exit"
    )
    ap.add_argument(
        "--stats", action="store_true", help="Print --campaign-id's progress stats and exit"
    )
    args = ap.parse_args()

    load_env()

    if args.pause or args.resume or args.stop or args.stats:
        if not args.campaign_id:
            raise SystemExit("--pause/--resume/--stop/--stats require --campaign-id")
        with (LeadStore(args.db) if args.db else LeadStore()) as store:
            if args.pause:
                pause_campaign(store, args.campaign_id)
                log.info("Campaign %s paused", args.campaign_id)
            if args.resume:
                resume_campaign(store, args.campaign_id)
                log.info("Campaign %s resumed", args.campaign_id)
            if args.stop:
                stop_campaign(store, args.campaign_id)
                log.info("Campaign %s stopped", args.campaign_id)
            if args.stats:
                print(format_campaign_stats(get_campaign_stats(store, args.campaign_id)))
        raise SystemExit(0)

    gmail = GmailSender()

    if args.test_email is not None:
        target = args.test_email or None
        result = gmail.send_test_email(target)
        if result.success:
            print(f"Test email sent (message_id={result.message_id})")
        else:
            log.error("Test email FAILED: %s", result.error)
        raise SystemExit(0 if result.success else 1)

    with (LeadStore(args.db) if args.db else LeadStore()) as store:
        campaign = load_campaign(store, args.campaign_id) if args.campaign_id else None
        stuck = list_stuck_sending(store, campaign_id=args.campaign_id)
        if stuck:
            log.warning(
                "%d lead(s) stuck in SENDING from a prior run -- review these "
                "(e.g. against Gmail's Sent folder) before continuing: %s",
                len(stuck),
                ", ".join(f"lead_id={s.lead_id}" for s in stuck),
            )

        run_state = get_run_state(store, args.campaign_id) if args.campaign_id else RUN_STATE_RUNNING
        if run_state != RUN_STATE_RUNNING:
            log.warning(
                "Campaign %s is %s -- not queueing or sending. Use --resume to continue.",
                args.campaign_id,
                run_state,
            )
            raise SystemExit(0)

        qstats = queue_pending_approvals(store, campaign_id=args.campaign_id)
        print(f"QUEUED:  {qstats['queued']} (skipped: {qstats['skipped']})")

        sstats = send_pending_queue(
            store,
            gmail,
            campaign=campaign,
            campaign_id=args.campaign_id,
            max_per_run=args.max_per_run,
            delay_seconds=args.delay_seconds,
        )
        print(f"SENT:    {sstats['sent']}")
        print(f"FAILED:  {sstats['failed']}")
        if sstats.get("halted"):
            log.warning("Run halted early -- campaign was paused or stopped mid-run.")
