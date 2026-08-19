from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Response

from app.config import ALLOW_LIVE_SEND
from app.db import database
from app.schemas.suppression import SuppressionCreate, SuppressionOut
from app.utils.security import provider_status

from pipeline import suppression

router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("")
def get_settings() -> dict:
    return {
        "providers": provider_status(),
        "live_sending_allowed": ALLOW_LIVE_SEND,
    }


@router.get("/suppressions", response_model=list[SuppressionOut])
def list_suppressions(campaign_id: str | None = Query(None)) -> list[SuppressionOut]:
    with database.get_store() as store:
        rows = suppression.list_suppressed(store, campaign_id=campaign_id)
    return [SuppressionOut(**row.to_dict()) for row in rows]


@router.post("/suppressions", response_model=SuppressionOut, status_code=201)
def create_suppression(payload: SuppressionCreate) -> SuppressionOut:
    with database.get_store() as store:
        try:
            contact = suppression.suppress_email(
                store, str(payload.email), reason=payload.reason, campaign_id=payload.campaign_id
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    return SuppressionOut(**contact.to_dict())


@router.delete("/suppressions/{email}", status_code=204, response_model=None)
def delete_suppression(email: str, campaign_id: str = Query("")) -> Response:
    # See campaigns.py's delete_campaign for why this must be
    # response_model=None + an explicit empty Response, not `-> None`.
    with database.get_store() as store:
        suppression.unsuppress_email(store, email, campaign_id=campaign_id)
    return Response(status_code=204)
