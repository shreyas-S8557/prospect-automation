from __future__ import annotations

from app.db import database
from app.schemas.analytics import (
    CampaignFunnelOut,
    CampaignStatsOut,
    DashboardStatsOut,
    FunnelStageOut,
)

from pipeline import campaign as campaign_pipeline
from pipeline.campaign_stats import STAT_FIELDNAMES, get_campaign_stats
from pipeline.models import PipelineStatus

_FUNNEL_STAGE_ORDER = [
    "discovered",
    "qualified",
    "email_found",
    "validated",
    "generated",
    "approved",
    "queued",
    "sent",
]

_REJECTION_STATUSES = [
    PipelineStatus.FILTERED_OUT,
    PipelineStatus.EMAIL_NOT_FOUND,
    PipelineStatus.VALIDATION_FAILED,
    PipelineStatus.GENERATION_FAILED,
    PipelineStatus.REJECTED,
    PipelineStatus.SEND_FAILED,
    PipelineStatus.CANCELLED,
]


def campaign_stats(campaign_id: str) -> CampaignStatsOut:
    with database.get_store() as store:
        stats = get_campaign_stats(store, campaign_id)
    return CampaignStatsOut(**{k: stats.get(k, 0) for k in STAT_FIELDNAMES})


def campaign_funnel(campaign_id: str) -> CampaignFunnelOut:
    with database.get_store() as store:
        stats = get_campaign_stats(store, campaign_id)
        status_counts = store.count_by_status(campaign_id=campaign_id)

    stages = []
    previous = None
    for name in _FUNNEL_STAGE_ORDER:
        count = stats.get(name, 0)
        drop_off = 0 if previous is None else max(previous - count, 0)
        stages.append(FunnelStageOut(stage=name, count=count, drop_off_from_previous=drop_off))
        previous = count

    rejection_reasons = {
        status.value: status_counts.get(status.value, 0)
        for status in _REJECTION_STATUSES
        if status_counts.get(status.value, 0) > 0
    }

    return CampaignFunnelOut(
        campaign_id=campaign_id, stages=stages, rejection_reasons=rejection_reasons
    )


def dashboard_stats() -> DashboardStatsOut:
    with database.get_store() as store:
        campaigns = campaign_pipeline.list_campaigns(store)
        active = sum(1 for c in campaigns if c.status == campaign_pipeline.CAMPAIGN_STATUS_ACTIVE)
        totals = get_campaign_stats(store, campaign_id=None)

    return DashboardStatsOut(
        total_campaigns=len(campaigns),
        active_campaigns=active,
        total_prospects=totals.get("discovered", 0),
        qualified_prospects=totals.get("qualified", 0),
        emails_found=totals.get("email_found", 0),
        emails_validated=totals.get("validated", 0),
        emails_approved=totals.get("approved", 0),
        emails_sent=totals.get("sent", 0),
    )
