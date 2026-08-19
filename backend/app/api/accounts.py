"""Account-level intelligence endpoints (Aug 2026 requirements): group a
campaign's discovered contacts by company, expose multi-contact/champion/
decision-maker-candidate signals per account, and an account-level funnel
alongside the existing prospect-level one. Purely read/derived -- no new
persisted state, computed from existing Lead records on every call.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.schemas.account import AccountFunnelOut, AccountListOut
from app.services import account_service, campaign_service

router = APIRouter(prefix="/api/campaigns", tags=["accounts"])


def _require_campaign(campaign_id: str) -> None:
    try:
        campaign_service.get_campaign(campaign_id)
    except campaign_service.CampaignNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id!r} not found") from exc


@router.get("/{campaign_id}/accounts", response_model=AccountListOut)
def list_accounts(campaign_id: str) -> AccountListOut:
    _require_campaign(campaign_id)
    return account_service.list_accounts(campaign_id)


@router.get("/{campaign_id}/accounts/funnel", response_model=AccountFunnelOut)
def account_funnel(campaign_id: str) -> AccountFunnelOut:
    _require_campaign(campaign_id)
    return account_service.get_account_funnel(campaign_id)
