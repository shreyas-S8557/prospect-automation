from __future__ import annotations

from pydantic import BaseModel, EmailStr


class SuppressionCreate(BaseModel):
    email: EmailStr
    reason: str = ""
    campaign_id: str = ""


class SuppressionOut(BaseModel):
    email_normalized: str
    campaign_id: str = ""
    reason: str = ""
    added_at: str = ""
