# API.md

REST API reference. Full interactive schema always available at
`/docs` (Swagger UI) once the backend is running. Base URL in local dev:
`http://localhost:8000`.

All endpoints return JSON. Errors follow FastAPI's standard shape:
`{"detail": "..."}`, with these status codes used consistently:

| Code | Meaning |
|---|---|
| 400 | Invalid input (e.g. bad age range) |
| 403 | Action not permitted (e.g. live sending disabled) |
| 404 | Resource not found |
| 409 | Invalid state transition (e.g. approving an already-approved email) |
| 422 | Request body failed schema/field validation |
| 500 | Unexpected server error (never includes a stack trace — see server logs) |

## Health

- `GET /api/health` — backend + database status
- `GET /api/health/providers` — configured/not-configured per external provider (never returns key values)

## Campaigns

- `POST /api/campaigns` — create
- `GET /api/campaigns?page=&page_size=` — paginated list
- `GET /api/campaigns/{campaign_id}`
- `PATCH /api/campaigns/{campaign_id}` — partial update (any subset of fields)
- `DELETE /api/campaigns/{campaign_id}` — archives (status → `archived`); never hard-deletes, so existing leads/emails/sends stay intact and attributable

Campaign fields: `campaign_name, description, target_titles, industries,
keywords, locations, exclude_keywords, company_size_min/max, age_min/max,
target_leads, target_count_mode, discovery_limit,
qualification_threshold, email_subject_template, email_body_template,
email_sender_name, email_validation_enabled, email_generation_enabled,
sending_enabled, send_mode (dry_run|test|live)`.

## Discovery / qualification (async — return a job immediately)

- `POST /api/campaigns/{id}/discover` → `{job_id, status}`
- `GET /api/campaigns/{id}/discovery-status` → discovery jobs for this campaign
- `POST /api/campaigns/{id}/qualify` → `{job_id, status}`

## Prospects

- `GET /api/campaigns/{id}/prospects?page=&page_size=&search=&pipeline_status=&industry=&title=&location=&sort_by=&sort_desc=`
- `GET /api/prospects/{lead_id}` — includes qualification evidence available on the Lead, email candidates, and the generated email if any
- `PATCH /api/prospects/{lead_id}`
- `DELETE /api/prospects/{lead_id}` — see DEVELOPMENT.md "Known limitations"

## Email pipeline (async)

- `POST /api/campaigns/{id}/find-emails` → job
- `POST /api/campaigns/{id}/validate-emails` → job
- `POST /api/campaigns/{id}/generate-emails` → job
- `GET /api/campaigns/{id}/emails?review_status=&page=&page_size=`
- `GET /api/emails/{lead_id}`
- `PATCH /api/emails/{lead_id}` — edit subject/body before approval
- `POST /api/emails/{lead_id}/approve`
- `POST /api/emails/{lead_id}/reject` — body: `{"reason": "..."}`
- `POST /api/campaigns/{id}/emails/approve-all`
- `POST /api/campaigns/{id}/emails/reject-all`

Review states follow the existing pipeline's state machine:
`PENDING → APPROVED → QUEUED → SENDING → SENT` (or `REJECTED` at
generation/approval time). Rejected emails can never enter the send
queue — enforced by `pipeline/models.py`'s `ALLOWED_TRANSITIONS`.

## Sending (async)

- `POST /api/campaigns/{id}/send` → job (403 if `send_mode=live` and the
  backend hasn't set `PROSPECT_ALLOW_LIVE_SEND=true`; 409 if
  `sending_enabled=false` on the campaign)
- `POST /api/campaigns/{id}/pause`
- `POST /api/campaigns/{id}/resume`
- `POST /api/campaigns/{id}/stop` — also cancels currently-QUEUED sends
- `GET /api/campaigns/{id}/sending-summary` → counts by send status

## Jobs

- `GET /api/jobs?campaign_id=` — list, newest first
- `GET /api/jobs/{job_id}` — full job state (see fields below)
- `POST /api/jobs/{job_id}/pause`
- `POST /api/jobs/{job_id}/resume`
- `POST /api/jobs/{job_id}/cancel`

Job fields: `id, campaign_id, type, status (queued|running|paused|
completed|failed|cancelled), phase, progress (0–1), total, processed,
successful, failed, message, started_at, completed_at, error, result`.
`progress`/`processed`/`successful`/`failed` are updated once per item
actually processed by the underlying pipeline function — never a
fabricated animation.

## Analytics

- `GET /api/dashboard/stats` — rollup across all campaigns
- `GET /api/campaigns/{id}/stats` — the ten funnel numbers from `pipeline/campaign_stats.py`
- `GET /api/campaigns/{id}/funnel` — same numbers as an ordered stage list with drop-off counts, plus a breakdown of terminal rejection reasons (`FILTERED_OUT`, `EMAIL_NOT_FOUND`, `VALIDATION_FAILED`, `GENERATION_FAILED`, `REJECTED`, `SEND_FAILED`, `CANCELLED`)

## Settings / suppression

- `GET /api/settings` — provider status + whether live sending is allowed
- `GET /api/settings/suppressions?campaign_id=`
- `POST /api/settings/suppressions` — body: `{"email", "reason", "campaign_id"}` (empty `campaign_id` = suppress everywhere)
- `DELETE /api/settings/suppressions/{email}?campaign_id=`
