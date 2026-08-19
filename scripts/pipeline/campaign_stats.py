"""Day 9: campaign progress/statistics (item 8).

get_campaign_stats() reports, for one campaign, exactly the ten numbers the
spec asks for:

    discovered, qualified, email_found, validated, generated, approved,
    queued, sent, failed, skipped

It is built entirely on top of LeadStore.count_by_status() (Lead's
pipeline_status is the single source of truth already maintained by every
earlier day's stage) -- this module adds no new writes anywhere, it only
reads and aggregates.

Two kinds of number are reported, and the docstrings below are explicit
about which is which:

  - discovered/qualified/email_found/validated/generated/approved/queued/
    sent are *cumulative funnel* counts: "how many leads have reached at
    least this milestone", not "how many are sitting in exactly this
    status right now" (the latter would make every early stage's count
    collapse toward zero as leads move on, which is not what a progress
    dashboard wants). See _MILESTONE_INDEX for the exact ordering and the
    conservative attribution used for terminal states with more than one
    possible predecessor (documented inline).
  - failed/skipped are *current-status* counts: failed is exactly "how
    many leads are sitting in SEND_FAILED right now", and skipped is every
    other non-error terminal exit (FILTERED_OUT, EMAIL_NOT_FOUND,
    VALIDATION_FAILED, GENERATION_FAILED, REJECTED, CANCELLED) added
    together -- these are meant as current snapshots of "what stopped and
    why", not funnel milestones.
"""

from __future__ import annotations

from .lead_store import LeadStore
from .models import PipelineStatus as S

# Ordered happy-path milestones, index = "how many stages completed".
_MILESTONE_ORDER: tuple[S, ...] = (
    S.DISCOVERED,               # 0
    S.QUALIFIED,                # 1
    S.EMAIL_CANDIDATES_FOUND,   # 2 ("email found")
    S.EMAIL_VALIDATED,          # 3
    S.EMAIL_GENERATED,          # 4
    S.APPROVED,                 # 5
    S.QUEUED,                   # 6
    S.SENDING,                  # 7 (no dedicated stat -- counts toward "queued")
    S.SENT,                     # 8
)
_MILESTONE_INDEX: dict[S, int] = {status: i for i, status in enumerate(_MILESTONE_ORDER)}

# Terminal exit states, attributed to the milestone index of the *last
# stage guaranteed to have been reached* before exiting. Several of these
# states have more than one legal predecessor (see models.py's
# ALLOWED_TRANSITIONS docstring) and a Lead's history isn't separately
# logged, so where the predecessor is ambiguous this deliberately picks the
# earliest (safest / most conservative) of the possible predecessors rather
# than guessing -- a milestone count can undercount a genuinely-reached
# stage for these leads, but will never overcount one they didn't reach.
_EXIT_MILESTONE: dict[S, int] = {
    # FILTERED_OUT: from DISCOVERED or QUALIFIED -> attribute to DISCOVERED.
    S.FILTERED_OUT: _MILESTONE_INDEX[S.DISCOVERED],
    # EMAIL_NOT_FOUND: from QUALIFIED or EMAIL_CANDIDATES_FOUND -> QUALIFIED.
    S.EMAIL_NOT_FOUND: _MILESTONE_INDEX[S.QUALIFIED],
    # VALIDATION_FAILED: only from EMAIL_CANDIDATES_FOUND -> unambiguous.
    S.VALIDATION_FAILED: _MILESTONE_INDEX[S.EMAIL_CANDIDATES_FOUND],
    # GENERATION_FAILED: only from EMAIL_VALIDATED -> unambiguous.
    S.GENERATION_FAILED: _MILESTONE_INDEX[S.EMAIL_VALIDATED],
    # REJECTED: from EMAIL_GENERATED or APPROVED -> attribute to
    # EMAIL_GENERATED (the earlier of the two).
    S.REJECTED: _MILESTONE_INDEX[S.EMAIL_GENERATED],
    # SEND_FAILED: only from SENDING -> reached (at least) QUEUED.
    S.SEND_FAILED: _MILESTONE_INDEX[S.QUEUED],
    # CANCELLED: only from QUEUED -> reached QUEUED.
    S.CANCELLED: _MILESTONE_INDEX[S.QUEUED],
}

# The stat keys, in report order, mapped to their milestone.
_FUNNEL_STATS: tuple[tuple[str, S], ...] = (
    ("discovered", S.DISCOVERED),
    ("qualified", S.QUALIFIED),
    ("email_found", S.EMAIL_CANDIDATES_FOUND),
    ("validated", S.EMAIL_VALIDATED),
    ("generated", S.EMAIL_GENERATED),
    ("approved", S.APPROVED),
    ("queued", S.QUEUED),
    ("sent", S.SENT),
)

_SKIPPED_STATUSES: tuple[S, ...] = (
    S.FILTERED_OUT,
    S.EMAIL_NOT_FOUND,
    S.VALIDATION_FAILED,
    S.GENERATION_FAILED,
    S.REJECTED,
    S.CANCELLED,
)

STAT_FIELDNAMES = [name for name, _ in _FUNNEL_STATS] + ["failed", "skipped"]


def _status_milestone(status: S) -> int:
    if status in _MILESTONE_INDEX:
        return _MILESTONE_INDEX[status]
    return _EXIT_MILESTONE[status]


def get_campaign_stats(store: LeadStore, campaign_id: str | None = None) -> dict[str, int]:
    """Return the ten Day-9 progress/statistics numbers for `campaign_id`
    (or across every campaign if None)."""
    counts = store.count_by_status(campaign_id=campaign_id)

    stats: dict[str, int] = {name: 0 for name in STAT_FIELDNAMES}
    for status_value, n in counts.items():
        try:
            status = S(status_value)
        except ValueError:
            continue  # defensive: an unrecognized status value is skipped

        milestone = _status_milestone(status)
        for name, threshold_status in _FUNNEL_STATS:
            if milestone >= _MILESTONE_INDEX[threshold_status]:
                stats[name] += n

        if status == S.SEND_FAILED:
            stats["failed"] += n
        elif status in _SKIPPED_STATUSES:
            stats["skipped"] += n

    return stats


def format_campaign_stats(stats: dict[str, int]) -> str:
    """A short, log-friendly rendering of get_campaign_stats()'s output."""
    width = max(len(name) for name in STAT_FIELDNAMES)
    lines = [f"{name.rjust(width)}: {stats.get(name, 0)}" for name in STAT_FIELDNAMES]
    return "\n".join(lines)
