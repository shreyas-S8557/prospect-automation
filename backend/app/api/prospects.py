from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response

from app.schemas.prospect import ProspectDetailOut, ProspectListOut, ProspectOut, ProspectUpdate
from app.services import campaign_service, prospect_service

router = APIRouter(tags=["prospects"])


@router.get("/api/campaigns/{campaign_id}/prospects", response_model=ProspectListOut)
def list_prospects(
    campaign_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    search: str | None = Query(None),
    status: str | None = Query(None, alias="pipeline_status"),
    industry: str | None = Query(None),
    title: str | None = Query(None),
    location: str | None = Query(None),
    sort_by: str = Query("updated_at"),
    sort_desc: bool = Query(True),
) -> ProspectListOut:
    try:
        campaign_service.get_campaign(campaign_id)
    except campaign_service.CampaignNotFound as exc:
        raise HTTPException(status_code=404, detail=f"Campaign {campaign_id!r} not found") from exc

    items, total = prospect_service.list_prospects(
        campaign_id,
        page=page,
        page_size=page_size,
        search=search,
        pipeline_status=status,
        industry=industry,
        title=title,
        location=location,
        sort_by=sort_by,
        sort_desc=sort_desc,
    )
    return ProspectListOut(items=items, page=page, page_size=page_size, total=total)


@router.get("/api/prospects/{lead_id}", response_model=ProspectDetailOut)
def get_prospect(lead_id: str) -> ProspectDetailOut:
    result = prospect_service.get_prospect(lead_id)
    if result is None:
        raise HTTPException(status_code=404, detail=f"Prospect {lead_id!r} not found")
    return result


@router.patch("/api/prospects/{lead_id}", response_model=ProspectOut)
def update_prospect(lead_id: str, payload: ProspectUpdate) -> ProspectOut:
    result = prospect_service.update_prospect(
        lead_id,
        job_title=payload.job_title,
        company_name=payload.company_name,
        email=payload.email,
    )
    if result is None:
        raise HTTPException(status_code=404, detail=f"Prospect {lead_id!r} not found")
    return result


@router.delete("/api/prospects/{lead_id}", status_code=204, response_model=None)
def delete_prospect(lead_id: str) -> Response:
    # See campaigns.py's delete_campaign for why this must be
    # response_model=None + an explicit empty Response, not `-> None`.
    if not prospect_service.delete_prospect(lead_id):
        raise HTTPException(status_code=404, detail=f"Prospect {lead_id!r} not found")
    return Response(status_code=204)
