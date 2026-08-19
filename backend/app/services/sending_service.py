from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from app.config import ALLOW_LIVE_SEND
from app.db import database
from app.workers.jobs import Job, JobControl, job_manager

from pipeline import campaign as campaign_pipeline
from pipeline import campaign_control
from pipeline import email_sending
from pipeline.gmail_sender import GmailSender, SendResult
from pipeline.models import PipelineStatus


class SendModeNotAllowed(PermissionError):
    pass


class DryRunGmailSender:
    """Drop-in replacement for GmailSender that never touches the network.

    Used for `dry_run` and `test` send modes (and always used for
    automated tests -- see PHASE 10/24 of the brief: "DO NOT send emails
    during automated tests"). Implements the same `.send()` contract
    (returns a SendResult) that email_sending.send_queued_email expects,
    so the exact same queueing/state-machine/retry code path is exercised
    without ever calling smtplib.
    """

    def send(self, to_email: str, subject: str, body: str, *, from_name: str = "") -> SendResult:
        return SendResult(success=True, message_id=f"dry-run-{uuid.uuid4().hex}", error="")


def _resolve_sender(send_mode: str) -> GmailSender | DryRunGmailSender:
    if send_mode == "live":
        if not ALLOW_LIVE_SEND:
            raise SendModeNotAllowed(
                "Live sending is disabled on this backend (PROSPECT_ALLOW_LIVE_SEND is not "
                "set). This is a deliberate safety default -- see DEVELOPMENT.md."
            )
        gmail = GmailSender()
        gmail.validate_credentials()
        return gmail
    return DryRunGmailSender()


def _run_send_job(job: Job, ctl: JobControl, campaign_id: str, send_mode: str) -> dict[str, Any]:
    ctl.set_phase("sending")
    sender = _resolve_sender(send_mode)
    with database.get_store() as store:
        template = campaign_pipeline.load_campaign(store, campaign_id)

        # Queue every APPROVED lead first.
        approved = store.list_by_status(PipelineStatus.APPROVED, campaign_id=campaign_id)
        for lead in approved:
            ctl.checkpoint()
            if campaign_control.get_run_state(store, campaign_id) != campaign_control.RUN_STATE_RUNNING:
                break
            try:
                email_sending.queue_approved_email(store, lead)
            except Exception:  # noqa: BLE001 -- one bad lead must not abort the batch
                continue

        queued = store.list_by_status(PipelineStatus.QUEUED, campaign_id=campaign_id)
        ctl.set_total(len(queued), phase="sending")
        sent = failed = 0
        for lead in queued:
            ctl.checkpoint()
            if campaign_control.get_run_state(store, campaign_id) != campaign_control.RUN_STATE_RUNNING:
                break
            updated = email_sending.send_queued_email(store, sender, lead.lead_id, campaign=template)
            ok = updated.status == PipelineStatus.SENT
            sent += int(ok)
            failed += int(not ok)
            ctl.advance(success=ok, message=updated.full_name or updated.lead_id)
    return {"sent": sent, "failed": failed, "send_mode": send_mode}


def start_sending(campaign_id: str, *, send_mode: str) -> Job:
    return job_manager.create(
        "sending", campaign_id, lambda job, ctl: _run_send_job(job, ctl, campaign_id, send_mode)
    )


def pause_campaign(campaign_id: str) -> dict[str, str]:
    with database.get_store() as store:
        control = campaign_control.pause_campaign(store, campaign_id)
    return control.to_dict()


def resume_campaign(campaign_id: str) -> dict[str, str]:
    with database.get_store() as store:
        control = campaign_control.resume_campaign(store, campaign_id)
    return control.to_dict()


def stop_campaign(campaign_id: str) -> dict[str, str]:
    with database.get_store() as store:
        control = campaign_control.stop_campaign(store, campaign_id)
    return control.to_dict()


def sending_summary(campaign_id: str) -> dict[str, int]:
    with database.get_store() as store:
        sends = store.list_email_sends(campaign_id=campaign_id)
    counts = {"queued": 0, "sending": 0, "sent": 0, "failed": 0, "cancelled": 0}
    status_map = {
        "QUEUED": "queued",
        "SENDING": "sending",
        "SENT": "sent",
        "SEND_FAILED": "failed",
        "CANCELLED": "cancelled",
    }
    for row in sends:
        key = status_map.get(row.get("send_status") or "")
        if key:
            counts[key] += 1
    return counts
