"""Day 9: suppression / do-not-contact list.

    suppress_email() -> persisted row in LeadStore's `suppressed_contacts`
        table (generic dict storage, same pattern as campaigns/email_jobs/
        email_sends -- see lead_store.py) -> is_suppressed() consulted by
        email_sending.queue_approved_email() before a lead is ever queued

A suppression can be global (applies to every campaign) or scoped to one
campaign_id. Addresses are matched case-insensitively and
whitespace-trimmed (normalize_email) so "Jane@Acme.com" and
" jane@acme.com " are treated as the same recipient.

Nothing here touches PipelineStatus or the Lead state machine -- a
suppressed lead is simply never queued (queue_approved_email raises
SuppressedRecipient, which queue_pending_approvals catches and counts as
skipped, exactly like NoApprovedEmailJob already does for Day 8). The lead
stays APPROVED rather than being force-transitioned into some new status,
so nothing about the existing, tested state machine in models.py changes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .lead_store import LeadStore
from .models import utc_now_iso


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


@dataclass
class SuppressedContact:
    email_normalized: str = ""
    campaign_id: str = ""
    reason: str = ""
    added_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SuppressedContact":
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in known and v is not None}
        return cls(**clean)


def suppress_email(
    store: LeadStore, email: str, *, reason: str = "", campaign_id: str = ""
) -> SuppressedContact:
    """Add `email` to the do-not-contact list.

    campaign_id="" (the default) suppresses the address everywhere; a
    specific campaign_id only suppresses it for that campaign. Idempotent:
    suppressing an already-suppressed (email, campaign_id) pair just
    updates the reason/timestamp rather than erroring or duplicating.
    """
    normalized = normalize_email(email)
    if not normalized:
        raise ValueError("Cannot suppress an empty email address")
    contact = SuppressedContact(
        email_normalized=normalized,
        campaign_id=campaign_id,
        reason=reason,
        added_at=utc_now_iso(),
    )
    store.save_suppressed_contact(contact.to_dict())
    return contact


def unsuppress_email(store: LeadStore, email: str, *, campaign_id: str = "") -> None:
    store.delete_suppressed_contact(normalize_email(email), campaign_id)


def is_suppressed(store: LeadStore, email: str, *, campaign_id: str = "") -> bool:
    """True if `email` is on the do-not-contact list, either globally or
    for the given campaign_id specifically."""
    normalized = normalize_email(email)
    if not normalized:
        return False
    if store.get_suppressed_contact(normalized, "") is not None:
        return True
    if campaign_id and store.get_suppressed_contact(normalized, campaign_id) is not None:
        return True
    return False


def list_suppressed(store: LeadStore, *, campaign_id: str | None = None) -> list[SuppressedContact]:
    return [
        SuppressedContact.from_dict(row)
        for row in store.list_suppressed_contacts(campaign_id=campaign_id)
    ]


# ---------------------------------------------------------------------------
# Duplicate-send protection: has this address already been sent to (or is a
# send for it already in flight), regardless of which lead_id it's attached
# to? Two different discovery runs can produce two different lead_ids for
# the same real person/address (see models.Lead.identity_key -- dedup is
# best-effort, not guaranteed), so this is a second, address-level guard on
# top of the lead-level UNIQUE(lead_id) constraint already enforced by Day 8.
# ---------------------------------------------------------------------------

_BLOCKING_SEND_STATUSES = ("QUEUED", "SENDING", "SENT")


def already_contacted(store: LeadStore, email: str, *, exclude_lead_id: str = "") -> bool:
    """True if `email` already has an in-flight or completed send
    (QUEUED/SENDING/SENT) under some other lead_id."""
    normalized = normalize_email(email)
    if not normalized:
        return False
    for row in store.find_email_sends_by_email(normalized):
        if row.get("lead_id") == exclude_lead_id:
            continue
        if row.get("send_status") in _BLOCKING_SEND_STATUSES:
            return True
    return False
