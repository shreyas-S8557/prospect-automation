# Architecture

This project is a lead-generation and email-outreach pipeline built as a
set of independent, unit-tested Python modules under `scripts/pipeline/`.
Every module owns one stage of a linear state machine and hands off through
a shared SQLite store — no module reaches into another's internals.

```
TargetConfig
      |
      v
Discovery (scripts/pipeline/sources/*.py)
      |  InvestorRow dicts (raw scraped/search results)
      v
lead_pipeline.normalize_investor_row / ingest_discovery_rows
      |  Lead (canonical model) -> DISCOVERED
      v
lead_pipeline.qualify_lead / qualify_pending_leads      -> QUALIFIED / FILTERED_OUT
      |
      v
LeadStore (lead_store.py, SQLite)  <-- every stage reads/writes leads here
      |
      v
email_discovery.process_lead_email                      -> EMAIL_CANDIDATES_FOUND / EMAIL_NOT_FOUND
      |
      v
email_validation.validate_and_select_email              -> EMAIL_VALIDATED / VALIDATION_FAILED
      |
      v
campaign.Campaign + email_generation.generate_email_for_lead -> EMAIL_GENERATED / GENERATION_FAILED
      |
      v
email_generation.approve_email / reject_email            -> APPROVED / REJECTED
      |
      v
email_sending.queue_approved_email                       -> QUEUED / (blocked by suppression/duplicate)
      |
      v
email_sending.send_queued_email -> gmail_sender.GmailSender -> SENDING -> SENT / SEND_FAILED
      |
      v
campaign_control.py (pause/resume/stop/limits/retry) + campaign_stats.py (funnel counters)
```

## Modules and responsibilities

| Module | Responsibility |
|---|---|
| `target_config.py` | `TargetConfig` dataclass — user-facing campaign criteria (location, titles, industries, keywords, company size, age proxy, target count). Presets (e.g. `CPA_PARTNER_PRESET`) are just pre-filled instances. |
| `sources/*.py` | Discovery adapters (DuckDuckGo search, Exa, HTTP seed lists, agentcrawl, crawl4ai). Each returns plain `InvestorRow` dicts; none of them know about `Lead` or pipeline state. |
| `quality.py` | `matches_target_criteria()` — the one place discovery rows are matched against a `TargetConfig`; also LinkedIn URL normalization/deduplication helpers. |
| `models.py` | `Lead` dataclass, `PipelineStatus` enum, and `ALLOWED_TRANSITIONS` — the explicit state machine every stage transitions through. Illegal transitions raise `InvalidStateTransition`. |
| `lead_pipeline.py` | Bridges `InvestorRow` -> `Lead`, dedup identity, `qualify_lead`/`qualify_pending_leads`. |
| `lead_store.py` | `LeadStore` — thin SQLite wrapper: leads, campaigns, email jobs, email sends, suppression list, campaign-control rows. One `.db` file per environment (or `:memory:` for tests). |
| `email_discovery.py` | Candidate email generation (pattern-based `ScrapegraphPatternGenerator`, optional vendor generators `MailfoguessGenerator`/`EmailFinderMainGenerator`), MX/SMTP checking (`NodeMXChecker`/`NodeSMTPChecker`, with `Null*` no-op variants for offline/testing), scoring and ranking. |
| `email_validation.py` | Selects the best validated candidate for a lead and moves it to `EMAIL_VALIDATED` or `VALIDATION_FAILED`. |
| `campaign.py` | `Campaign` dataclass, template validation (only 6 whitelisted `{{variable}}` names), `create_campaign`/`save_campaign`/`load_campaign`. |
| `email_generation.py` | Renders a `Campaign` template against a `Lead` into subject/body, persists an `EmailJob`, and drives review (`approve_email`/`reject_email`/bulk variants). |
| `email_sending.py` | Queues approved jobs (`queue_approved_email`, blocked by suppression/duplicate checks), sends via `GmailSender` (`send_queued_email`/`send_pending_queue`), and recovers stuck `SENDING` rows after a crash (`list_stuck_sending`, `mark_stuck_as_failed`, `resolve_stuck_as_sent`). |
| `gmail_sender.py` | `GmailSender` — thin adapter over `smtplib.SMTP_SSL`, credentials from `GMAIL_ADDRESS`/`GMAIL_APP_PASSWORD` env vars, SMTP client injectable for tests. |
| `suppression.py` | Do-not-contact list (`suppress_email`/`unsuppress_email`/`is_suppressed`) and cross-lead duplicate-send detection (`already_contacted`). |
| `campaign_control.py` | Pause/resume/stop a campaign, per-campaign send limits/delay/retry settings (`effective_send_settings`). |
| `campaign_stats.py` | `get_campaign_stats()` — cumulative funnel counters (discovered -> ... -> sent) plus `failed`/`skipped` totals for a campaign. |

## State machine

`models.ALLOWED_TRANSITIONS` is the single source of truth for what
transitions are legal. The happy path is strictly linear:

```
DISCOVERED -> QUALIFIED -> EMAIL_CANDIDATES_FOUND -> EMAIL_VALIDATED
  -> EMAIL_GENERATED -> APPROVED -> QUEUED -> SENDING -> SENT
```

Every other status (`FILTERED_OUT`, `EMAIL_NOT_FOUND`, `VALIDATION_FAILED`,
`GENERATION_FAILED`, `REJECTED`, `SEND_FAILED`, `CANCELLED`) is a terminal
exit reached from a specific point in the happy path — a lead can never
skip a stage or move backwards out of a terminal state; `store.transition()`
enforces this against `ALLOWED_TRANSITIONS` on every write.

## Persistence

Everything lives in one SQLite database (`LeadStore`), with a distinct
table per concept: `leads`, `campaigns`, `email_jobs`, `email_sends`,
`suppression`, `campaign_control`. Every write in `LeadStore` is a single
committed transaction, and the QUEUED->SENDING transition is written to the
`email_sends` table *before* the network call in `send_queued_email`, so a
crash mid-send leaves a visibly `SENDING` row rather than losing or
duplicating the attempt (see `list_stuck_sending`/`resolve_stuck_as_sent`
for the recovery path).

## Dependency boundary

Everything through `send_queued_email` runs on the Python standard library
plus `pyyaml`/`python-dotenv`. Only the *discovery* sources
(`sources/exa_search.py`, LLM-based classification in `llm.py`, the
DuckDuckGo/agentcrawl/crawl4ai adapters) pull in optional third-party
packages (`openai`, `httpx`, `ddgs`, `agentcrawl-ai`, `crawl4ai`,
`playwright`) — none of which are required to run the qualification →
email → campaign → send pipeline against leads that are already in the
store. See `HOW_TO_USE.md` for the exact install matrix.

---

# Application layer (backend + frontend)

Everything above this line describes `scripts/pipeline/` exactly as it
existed before this pass — **nothing in it was rewritten**. This section
documents the orchestration/application layer added around it.

```
                  frontend/index.html
              (static HTML/CSS/JS, no build step)
                          |
                     HTTP / REST (fetch)
                          |
                          v
                 backend/app/main.py (FastAPI)
                          |
        +-----------------+------------------+
        |                 |                  |
        v                 v                  v
  app/api/*.py      app/services/*.py   app/workers/jobs.py
  (routers,          (map API<->pipeline   (ThreadPoolExecutor,
   validation,        calls, one file       real per-item progress,
   HTTP concerns)      per pipeline stage)   pause/resume/cancel)
        |                 |
        |                 v
        |         scripts/pipeline/*  <-- UNCHANGED. Imported via
        |         (Lead state machine,     app/pipeline_bridge.py,
        |          LeadStore/SQLite,       which only adds scripts/
        |          campaign/email/         to sys.path and loads .env.
        |          sending logic)
        v
  app/db/database.py
  -- reuses pipeline.lead_store.LeadStore directly for all existing
     tables (leads, campaigns, email_candidates, email_jobs,
     email_sends, campaign_controls, suppressed_contacts)
  -- adds exactly ONE new table, `app_campaign_configs`, in the SAME
     SQLite file, to hold the *targeting* half of a campaign
     (TargetConfig JSON + discovery_limit + feature toggles) that the
     existing schema had no place for. Nothing in scripts/pipeline/ is
     aware of this table.
```

## Why this shape

- **The pipeline owns all business logic.** The backend's service layer
  (`app/services/*.py`) is a thin adapter: it calls the *existing*
  per-lead functions (`process_lead_email`, `validate_and_select_email`,
  `generate_email_for_lead`, `queue_approved_email`,
  `send_queued_email`, `qualify_lead`, ...) in a loop, rather than the
  bulk `find_and_score_pending_leads`-style wrappers, specifically so the
  job system can report real per-item progress and honor pause/cancel
  between items — without touching a single line of those functions.
- **One SQLite file, one source of truth.** A campaign/lead/email created
  via the CLI scripts documented in the root `README.md` is immediately
  visible through the API and UI, and vice versa — there is no import/
  export step and no data duplication.
- **No Redis/Celery.** `app/workers/jobs.py` is a small, explicit
  `ThreadPoolExecutor`-backed job manager. It's structured so the
  `submit/get/pause/resume/cancel` contract could be swapped onto
  Celery/RQ later without changing any service-layer call site, but a
  single-process in-memory manager is all a local, single-user deployment
  needs today (see PHASE 5 of the original brief).
- **Sending safety is enforced twice.** Once synchronously in the API
  router (fails fast, before a job is even created) and once inside the
  job worker itself (defense in depth) — a `live` send is refused unless
  the backend process has `PROSPECT_ALLOW_LIVE_SEND=true` set explicitly.
  Automated tests never set this, so they can never send real email.
