"""Day 9: campaign-level run-state (pause/resume/stop) + sending controls.

    RUNNING <--pause_campaign()--> PAUSED
       |                              |
       +---------- stop_campaign() ---+
                        |
                        v
                    STOPPED (terminal)

This module owns a small, additive `campaign_controls` table (see
lead_store.py's "Day 9" section) that is entirely separate from the
Campaign template record in campaign.py -- nothing here changes
Campaign/create_campaign/render_template, and campaign.py is not imported
by (or aware of) this module. A campaign with no campaign_controls row is
simply treated as RUNNING with the module-level sending defaults from
email_sending.py (DEFAULT_MAX_PER_RUN / DEFAULT_DELAY_SECONDS /
DEFAULT_MAX_RETRIES / DEFAULT_RETRY_BACKOFF_SECONDS) -- so every campaign
created before Day 9 keeps working exactly as before, unpaused, with the
same defaults it already had.

Enforcement lives in email_sending.py: queue_pending_approvals() and
send_pending_queue() check `get_run_state()` before each item and stop
early (without raising) the moment a campaign is no longer RUNNING, so
pausing/stopping a campaign takes effect on the very next lead in a batch,
not just before the batch starts.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .lead_store import LeadStore
from .models import utc_now_iso

RUN_STATE_RUNNING = "RUNNING"
RUN_STATE_PAUSED = "PAUSED"
RUN_STATE_STOPPED = "STOPPED"

_VALID_RUN_STATES = frozenset({RUN_STATE_RUNNING, RUN_STATE_PAUSED, RUN_STATE_STOPPED})


class CampaignAlreadyStopped(ValueError):
    """Raised by pause_campaign/resume_campaign once a campaign is STOPPED
    (a terminal state -- a stopped campaign can never run again; create a
    new campaign instead)."""

    def __init__(self, campaign_id: str):
        self.campaign_id = campaign_id
        super().__init__(
            f"Campaign {campaign_id!r} is STOPPED and cannot be paused/resumed"
        )


@dataclass
class CampaignControl:
    """Run-state + sending-config overrides for one campaign.

    max_per_run/delay_seconds/max_retries/retry_backoff_seconds are stored
    as strings (blank = "not configured, use the module default") purely so
    this round-trips through SQLite exactly like Lead/Campaign/EmailSend
    already do -- effective_send_settings() is what converts blanks into
    real numeric defaults.
    """

    campaign_id: str = ""
    run_state: str = RUN_STATE_RUNNING
    max_per_run: str = ""
    delay_seconds: str = ""
    max_retries: str = ""
    retry_backoff_seconds: str = ""
    paused_at: str = ""
    resumed_at: str = ""
    stopped_at: str = ""
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CampaignControl":
        known = {f for f in cls.__dataclass_fields__}
        clean = {k: v for k, v in data.items() if k in known and v is not None}
        return cls(**clean)


def get_campaign_control(store: LeadStore, campaign_id: str) -> CampaignControl:
    """Always returns a CampaignControl -- a campaign with no persisted row
    is RUNNING with every setting unconfigured (module defaults apply)."""
    row = store.get_campaign_control(campaign_id)
    if row is None:
        return CampaignControl(campaign_id=campaign_id)
    return CampaignControl.from_dict(row)


def get_run_state(store: LeadStore, campaign_id: str) -> str:
    if not campaign_id:
        return RUN_STATE_RUNNING
    return get_campaign_control(store, campaign_id).run_state


def _save(store: LeadStore, control: CampaignControl) -> CampaignControl:
    control.updated_at = utc_now_iso()
    store.save_campaign_control(control.to_dict())
    return control


def pause_campaign(store: LeadStore, campaign_id: str) -> CampaignControl:
    """RUNNING -> PAUSED. Idempotent if already PAUSED. Raises
    CampaignAlreadyStopped if the campaign was explicitly stopped."""
    control = get_campaign_control(store, campaign_id)
    if control.run_state == RUN_STATE_STOPPED:
        raise CampaignAlreadyStopped(campaign_id)
    control.campaign_id = campaign_id
    control.run_state = RUN_STATE_PAUSED
    control.paused_at = utc_now_iso()
    if not control.created_at:
        control.created_at = control.paused_at
    return _save(store, control)


def resume_campaign(store: LeadStore, campaign_id: str) -> CampaignControl:
    """PAUSED -> RUNNING. Idempotent if already RUNNING. Raises
    CampaignAlreadyStopped if the campaign was explicitly stopped."""
    control = get_campaign_control(store, campaign_id)
    if control.run_state == RUN_STATE_STOPPED:
        raise CampaignAlreadyStopped(campaign_id)
    control.campaign_id = campaign_id
    control.run_state = RUN_STATE_RUNNING
    control.resumed_at = utc_now_iso()
    if not control.created_at:
        control.created_at = control.resumed_at
    return _save(store, control)


def stop_campaign(
    store: LeadStore, campaign_id: str, *, cancel_queued: bool = True
) -> CampaignControl:
    """RUNNING or PAUSED -> STOPPED (terminal). Idempotent if already
    STOPPED.

    By default (cancel_queued=True) also explicitly cancels every
    currently-QUEUED send for this campaign -- both the Lead
    (QUEUED -> CANCELLED, already a legal edge in models.ALLOWED_TRANSITIONS)
    and its EmailSend row (send_status -> CANCELLED) -- so "stop" actually
    stops outstanding queued work rather than merely blocking new work from
    starting. Leads already SENDING/SENT/SEND_FAILED are untouched: a stop
    can't un-send something, and a lead already mid-send is left for crash-
    recovery handling (see email_sending.list_stuck_sending), not silently
    reclassified.
    """
    from . import email_sending  # local import: avoids a module-load cycle
    from .models import PipelineStatus

    control = get_campaign_control(store, campaign_id)
    control.campaign_id = campaign_id
    already_stopped = control.run_state == RUN_STATE_STOPPED
    control.run_state = RUN_STATE_STOPPED
    control.stopped_at = utc_now_iso()
    if not control.created_at:
        control.created_at = control.stopped_at
    _save(store, control)

    if cancel_queued and not already_stopped:
        for row in store.list_email_sends(
            campaign_id=campaign_id, send_status=email_sending.SEND_QUEUED
        ):
            send = email_sending.EmailSend.from_dict(row)
            send.send_status = email_sending.SEND_CANCELLED
            send.updated_at = utc_now_iso()
            store.save_email_send(send.to_dict())
            store.transition(send.lead_id, PipelineStatus.CANCELLED)

    return control


def configure_sending(
    store: LeadStore,
    campaign_id: str,
    *,
    max_per_run: int | None = None,
    delay_seconds: float | None = None,
    max_retries: int | None = None,
    retry_backoff_seconds: float | None = None,
) -> CampaignControl:
    """Persist per-campaign overrides for the sending controls (items 3-5:
    per-run limit, inter-send delay, retries). Only the settings explicitly
    passed are changed; omitted ones keep whatever was previously
    configured (or stay unconfigured, i.e. "use the module default")."""
    control = get_campaign_control(store, campaign_id)
    control.campaign_id = campaign_id
    if max_per_run is not None:
        control.max_per_run = str(int(max_per_run))
    if delay_seconds is not None:
        control.delay_seconds = str(float(delay_seconds))
    if max_retries is not None:
        control.max_retries = str(int(max_retries))
    if retry_backoff_seconds is not None:
        control.retry_backoff_seconds = str(float(retry_backoff_seconds))
    if not control.created_at:
        control.created_at = utc_now_iso()
    return _save(store, control)


def effective_send_settings(
    store: LeadStore,
    campaign_id: str | None,
    *,
    max_per_run: int | None,
    delay_seconds: float | None,
    max_retries: int | None,
    retry_backoff_seconds: float | None,
    default_max_per_run: int,
    default_delay_seconds: float,
    default_max_retries: int,
    default_retry_backoff_seconds: float,
) -> tuple[int, float, int, float]:
    """Resolve the effective (max_per_run, delay_seconds, max_retries,
    retry_backoff_seconds) for one call, in priority order:

        1. an explicit argument passed to this call (highest priority --
           an explicit override always wins)
        2. a per-campaign value persisted via configure_sending()
        3. the caller-supplied module default (lowest priority)

    Every caller keeps working with zero config: with no campaign_id and no
    persisted control row, this simply returns the four defaults passed in,
    exactly matching pre-Day-9 behavior.
    """
    control = get_campaign_control(store, campaign_id) if campaign_id else CampaignControl()

    def _resolve(explicit, stored, default, caster):
        if explicit is not None:
            return caster(explicit)
        if stored:
            return caster(stored)
        return default

    return (
        _resolve(max_per_run, control.max_per_run, default_max_per_run, int),
        _resolve(delay_seconds, control.delay_seconds, default_delay_seconds, float),
        _resolve(max_retries, control.max_retries, default_max_retries, int),
        _resolve(
            retry_backoff_seconds,
            control.retry_backoff_seconds,
            default_retry_backoff_seconds,
            float,
        ),
    )
