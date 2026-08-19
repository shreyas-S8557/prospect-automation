from __future__ import annotations

import uuid
from typing import Any

from app.db import database
from app.schemas.campaign import CampaignCreate, CampaignOut, CampaignUpdate

from pipeline import campaign as campaign_pipeline
from pipeline import campaign_control
from pipeline.lead_store import LeadStore
from pipeline.models import utc_now_iso
from pipeline.target_config import TargetConfig


class CampaignNotFound(LookupError):
    pass


def _slugify(name: str) -> str:
    base = "".join(c.lower() if c.isalnum() else "-" for c in name).strip("-")
    base = "-".join(filter(None, base.split("-")))
    return base or uuid.uuid4().hex[:8]


def _target_config_from_payload(payload: dict[str, Any]) -> TargetConfig:
    return TargetConfig(
        locations=payload.get("locations") or ["United States"],
        titles=payload.get("target_titles") or [],
        industries=payload.get("industries") or [],
        keywords=payload.get("keywords") or [],
        exclude_keywords=payload.get("exclude_keywords") or [],
        company_size_min=payload.get("company_size_min"),
        company_size_max=payload.get("company_size_max"),
        age_min=payload.get("age_min"),
        age_max=payload.get("age_max"),
        target_count=payload.get("target_leads") or 50,
        target_count_mode=payload.get("target_count_mode") or "raw",
        name=payload.get("campaign_name") or "campaign",
    )


def _compose(store: LeadStore, campaign_id: str) -> CampaignOut | None:
    template = campaign_pipeline.load_campaign(store, campaign_id)
    if template is None:
        return None
    app_cfg = database.get_campaign_config(campaign_id) or {}
    tc = app_cfg.get("target_config", {})
    control = campaign_control.get_campaign_control(store, campaign_id)

    return CampaignOut(
        campaign_id=campaign_id,
        campaign_name=template.name,
        description=template.description,
        status=template.status,
        target_titles=tc.get("titles", []),
        industries=tc.get("industries", []),
        keywords=tc.get("keywords", []),
        locations=tc.get("locations", ["United States"]),
        exclude_keywords=tc.get("exclude_keywords", []),
        company_size_min=tc.get("company_size_min"),
        company_size_max=tc.get("company_size_max"),
        age_min=tc.get("age_min"),
        age_max=tc.get("age_max"),
        target_leads=tc.get("target_count", 0),
        target_count_mode=tc.get("target_count_mode", "raw"),
        discovery_limit=app_cfg.get("discovery_limit", 200),
        qualification_threshold=app_cfg.get("qualification_threshold", ""),
        email_subject_template=template.subject_template,
        email_body_template=template.body_template,
        email_sender_name=template.sender_name,
        case_studies=template.case_studies,
        pain_points=template.pain_points,
        email_validation_enabled=bool(app_cfg.get("email_validation_enabled", 1)),
        email_generation_enabled=bool(app_cfg.get("email_generation_enabled", 1)),
        sending_enabled=bool(app_cfg.get("sending_enabled", 0)),
        send_mode=app_cfg.get("send_mode", "dry_run"),
        run_state=control.run_state,
        created_at=template.created_at,
        updated_at=template.updated_at,
    )


def create_campaign(payload: CampaignCreate) -> CampaignOut:
    campaign_id = f"{_slugify(payload.campaign_name)}-{uuid.uuid4().hex[:6]}"
    target = _target_config_from_payload(payload.model_dump())

    with database.get_store() as store:
        template = campaign_pipeline.create_campaign(
            payload.campaign_name,
            payload.email_subject_template or campaign_pipeline.SHARP_SUBJECT_TEMPLATE,
            payload.email_body_template or campaign_pipeline.SHARP_BODY_TEMPLATE,
            description=payload.description,
            sender_name=payload.email_sender_name,
            campaign_id=campaign_id,
            case_studies=payload.case_studies,
            pain_points=payload.pain_points,
        )
        campaign_pipeline.save_campaign(store, template)

        now = utc_now_iso()
        database.save_campaign_config(
            campaign_id,
            {
                "target_config": target.to_dict(),
                "discovery_limit": payload.discovery_limit,
                "qualification_threshold": payload.qualification_threshold,
                "email_validation_enabled": int(payload.email_validation_enabled),
                "email_generation_enabled": int(payload.email_generation_enabled),
                "sending_enabled": int(payload.sending_enabled),
                "send_mode": payload.send_mode,
                "created_at": now,
                "updated_at": now,
            },
        )
        return _compose(store, campaign_id)


def get_campaign(campaign_id: str) -> CampaignOut:
    with database.get_store() as store:
        out = _compose(store, campaign_id)
        if out is None:
            raise CampaignNotFound(campaign_id)
        return out


def list_campaigns(*, page: int = 1, page_size: int = 50) -> tuple[list[CampaignOut], int]:
    with database.get_store() as store:
        all_rows = campaign_pipeline.list_campaigns(store)
        total = len(all_rows)
        start = (page - 1) * page_size
        page_rows = all_rows[start : start + page_size]
        items = [_compose(store, c.campaign_id) for c in page_rows]
        return [i for i in items if i is not None], total


def update_campaign(campaign_id: str, payload: CampaignUpdate) -> CampaignOut:
    with database.get_store() as store:
        template = campaign_pipeline.load_campaign(store, campaign_id)
        if template is None:
            raise CampaignNotFound(campaign_id)
        app_cfg = database.get_campaign_config(campaign_id) or {"target_config": {}}
        tc = dict(app_cfg.get("target_config", {}))

        data = payload.model_dump(exclude_unset=True)

        if "campaign_name" in data:
            template.name = data["campaign_name"]
        if "description" in data:
            template.description = data["description"]
        if "status" in data:
            template.status = data["status"]
        if "email_subject_template" in data and data["email_subject_template"]:
            campaign_pipeline.validate_template(data["email_subject_template"])
            template.subject_template = data["email_subject_template"]
        if "email_body_template" in data and data["email_body_template"]:
            campaign_pipeline.validate_template(data["email_body_template"])
            template.body_template = data["email_body_template"]
        if "email_sender_name" in data:
            template.sender_name = data["email_sender_name"]
        if "case_studies" in data and data["case_studies"] is not None:
            template.case_studies = data["case_studies"]
        if "pain_points" in data and data["pain_points"] is not None:
            template.pain_points = data["pain_points"]
        template.updated_at = utc_now_iso()
        campaign_pipeline.save_campaign(store, template)

        field_map = {
            "target_titles": "titles",
            "industries": "industries",
            "keywords": "keywords",
            "locations": "locations",
            "exclude_keywords": "exclude_keywords",
            "company_size_min": "company_size_min",
            "company_size_max": "company_size_max",
            "age_min": "age_min",
            "age_max": "age_max",
            "target_leads": "target_count",
            "target_count_mode": "target_count_mode",
        }
        for api_field, tc_field in field_map.items():
            if api_field in data:
                tc[tc_field] = data[api_field]
        tc.setdefault("name", template.name)
        # Re-validate via TargetConfig so bad ranges (e.g. age_min > age_max)
        # are rejected at update time, not silently persisted.
        validated = TargetConfig(
            locations=tc.get("locations") or ["United States"],
            titles=tc.get("titles") or [],
            industries=tc.get("industries") or [],
            keywords=tc.get("keywords") or [],
            exclude_keywords=tc.get("exclude_keywords") or [],
            company_size_min=tc.get("company_size_min"),
            company_size_max=tc.get("company_size_max"),
            age_min=tc.get("age_min"),
            age_max=tc.get("age_max"),
            target_count=tc.get("target_count") or 50,
            target_count_mode=tc.get("target_count_mode") or "raw",
            name=tc.get("name") or template.name,
        )

        new_app_cfg = {
            "target_config": validated.to_dict(),
            "discovery_limit": data.get("discovery_limit", app_cfg.get("discovery_limit", 200)),
            "qualification_threshold": data.get(
                "qualification_threshold", app_cfg.get("qualification_threshold", "")
            ),
            "email_validation_enabled": int(
                data.get(
                    "email_validation_enabled",
                    bool(app_cfg.get("email_validation_enabled", 1)),
                )
            ),
            "email_generation_enabled": int(
                data.get(
                    "email_generation_enabled",
                    bool(app_cfg.get("email_generation_enabled", 1)),
                )
            ),
            "sending_enabled": int(
                data.get("sending_enabled", bool(app_cfg.get("sending_enabled", 0)))
            ),
            "send_mode": data.get("send_mode", app_cfg.get("send_mode", "dry_run")),
            "created_at": app_cfg.get("created_at", utc_now_iso()),
            "updated_at": utc_now_iso(),
        }
        database.save_campaign_config(campaign_id, new_app_cfg)

        return _compose(store, campaign_id)


def delete_campaign(campaign_id: str) -> None:
    with database.get_store() as store:
        template = campaign_pipeline.load_campaign(store, campaign_id)
        if template is None:
            raise CampaignNotFound(campaign_id)
        # The pipeline's Campaign table has no delete helper (by design --
        # leads reference campaign_id and losing the template record would
        # orphan them). We archive instead of hard-deleting, which is a
        # safe, reversible operation and keeps every existing lead/email
        # record intact and attributable.
        template.status = campaign_pipeline.CAMPAIGN_STATUS_ARCHIVED
        template.updated_at = utc_now_iso()
        campaign_pipeline.save_campaign(store, template)
        campaign_control.stop_campaign(store, campaign_id, cancel_queued=True)


def get_target_config(campaign_id: str) -> TargetConfig:
    app_cfg = database.get_campaign_config(campaign_id)
    if app_cfg is None:
        raise CampaignNotFound(campaign_id)
    return TargetConfig.from_dict(app_cfg["target_config"])


def get_app_config(campaign_id: str) -> dict[str, Any]:
    app_cfg = database.get_campaign_config(campaign_id)
    if app_cfg is None:
        raise CampaignNotFound(campaign_id)
    return app_cfg
