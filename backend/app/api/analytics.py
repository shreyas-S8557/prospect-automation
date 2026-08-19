from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.schemas.analytics import CampaignFunnelOut, CampaignStatsOut, DashboardStatsOut
from app.services import analytics_service, campaign_service

router = APIRouter(tags=["analytics"])


@router.get("/api/dashboard/stats", response_model=DashboardStatsOut)
def dashboard_stats() -> DashboardStatsOut:
    return analytics_service.dashboard_stats()


@router.get("/api/campaigns/{campaign_id}/stats", response_model=CampaignStatsOut)
def campaign_stats(campaign_id: str) -> CampaignStatsOut:
    try:
        campaign_service.get_campaign(campaign_id)
    except campaign_service.CampaignNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id!r} not found") from exc
    return analytics_service.campaign_stats(campaign_id)


@router.get("/api/campaigns/{campaign_id}/funnel", response_model=CampaignFunnelOut)
def campaign_funnel(campaign_id: str) -> CampaignFunnelOut:
    try:
        campaign_service.get_campaign(campaign_id)
    except campaign_service.CampaignNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id!r} not found") from exc
    return analytics_service.campaign_funnel(campaign_id)
