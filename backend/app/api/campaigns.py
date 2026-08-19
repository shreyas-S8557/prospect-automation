from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response

from app.schemas.campaign import CampaignCreate, CampaignListOut, CampaignOut, CampaignUpdate
from app.services import campaign_service
from pipeline.campaign import UnsupportedTemplateVariable

router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


@router.post("", response_model=CampaignOut, status_code=201)
def create_campaign(payload: CampaignCreate) -> CampaignOut:
    try:
        return campaign_service.create_campaign(payload)
    except UnsupportedTemplateVariable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("", response_model=CampaignListOut)
def list_campaigns(page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=200)) -> CampaignListOut:
    items, total = campaign_service.list_campaigns(page=page, page_size=page_size)
    return CampaignListOut(items=items, page=page, page_size=page_size, total=total)


@router.get("/{campaign_id}", response_model=CampaignOut)
def get_campaign(campaign_id: str) -> CampaignOut:
    try:
        return campaign_service.get_campaign(campaign_id)
    except campaign_service.CampaignNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id!r} not found") from exc


@router.patch("/{campaign_id}", response_model=CampaignOut)
def update_campaign(campaign_id: str, payload: CampaignUpdate) -> CampaignOut:
    try:
        return campaign_service.update_campaign(campaign_id, payload)
    except campaign_service.CampaignNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id!r} not found") from exc
    except UnsupportedTemplateVariable as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/{campaign_id}", status_code=204, response_model=None)
def delete_campaign(campaign_id: str) -> Response:
    # BUGFIX (Windows startup crash): this endpoint used to be
    # `def delete_campaign(campaign_id: str) -> None:` with no explicit
    # response_model. FastAPI infers response_model from the return type
    # annotation when one isn't given explicitly, and on some FastAPI/
    # Pydantic version combinations a `-> None` annotation gets turned
    # into a real `NoneType` response_model (not treated as "no model"),
    # which then fails FastAPI's own assertion that a 204 response must
    # not have a body -- this crashes the whole app at IMPORT time
    # (before any request is even made), which is why it surfaced as a
    # uvicorn startup failure rather than a runtime error.
    #
    # Fix: pass response_model=None explicitly (guarantees FastAPI never
    # tries to infer/attach a response body schema for this route,
    # regardless of version) and return a concrete, empty Response
    # instead of relying on an implicit `None` return being converted
    # into a body. This is the FastAPI-maintainer-recommended pattern for
    # a guaranteed-empty 204 endpoint and is safe across FastAPI versions.
    try:
        campaign_service.delete_campaign(campaign_id)
    except campaign_service.CampaignNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id!r} not found") from exc
    return Response(status_code=204)
