# DEVELOPMENT.md

How to run the full application locally: FastAPI backend + the static
frontend, on top of the existing, untouched `scripts/pipeline` package.

## 1. Backend

```bash
cd backend
pip install -r requirements.txt --break-system-packages   # or use a venv
uvicorn app.main:app --reload --port 8000
```

The backend uses the **same SQLite file** the CLI pipeline already uses
(`data/pipeline_state.db` by default) — anything created via the CLI
scripts documented in the root `README.md` shows up in the API/UI, and
vice versa. Override the path with:

```bash
export PROSPECT_DB_PATH=/path/to/other.db
```

Environment variables (all optional, same ones the CLI pipeline already
reads via `.env` — see `.env.example`):

| Variable | Purpose |
|---|---|
| `OPENAI_API_KEY`, `OPENAI_BASE_URL` | LLM parse/classify (discovery) |
| `EXA_API_KEY` | Optional Exa search source |
| `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD` | Required only for **live** sending |
| `PROSPECT_DB_PATH` | Override the SQLite file path |
| `PROSPECT_CORS_ORIGINS` | Comma-separated list of allowed frontend origins |
| `PROSPECT_ALLOW_LIVE_SEND` | Must be `true` for the backend to ever send real email — **off by default** |
| `PROSPECT_LOG_LEVEL` | Default `INFO` |

Interactive API docs: `http://localhost:8000/docs`

## 2. Frontend

The frontend is a **single static HTML file** — no npm, no build step, no
framework. Serve it with any static file server:

```bash
cd frontend
python3 -m http.server 8080
```

Then open `http://localhost:8080`. On load, it talks to the backend at the
URL shown in the top-right "API base" field (defaults to
`http://localhost:8000` — change it there if your backend runs elsewhere).

You can also just double-click `frontend/index.html` to open it directly
(`file://`) — the backend's CORS config allows `null` origins for this.

## 3. Tests

```bash
# Existing pipeline test suite (untouched, scripts/pipeline)
cd scripts
python3 -m pytest pipeline/ -q

# New backend test suite (isolated temp SQLite DB per test, no network,
# no real email ever sent)
cd backend
python3 -m pytest tests/ -q
```

## 4. Live provider checks (never run automatically)

The automated test suites above use mocks/dry-run only. To check real
external providers, use the CLI pipeline's normal commands directly (see
root `README.md` / `HOW_TO_USE.md`) — e.g.:

```bash
cd scripts
python3 -m pipeline.orchestrator --config ../data/target_configs/saas_founders.json --phases ddgs,classify
```

There is no separate `scripts/live_test.py` harness in this pass —
see "Remaining issues" in the final report for how to add one following
the same pattern as the existing per-stage CLI scripts.

## 5. Sending safety

- Every campaign has a `send_mode`: `dry_run` (default, simulates success,
  never touches the network — this is what all automated tests use),
  `test`, or `live`.
- Even a campaign configured for `live` sending is refused by the backend
  unless the operator explicitly sets `PROSPECT_ALLOW_LIVE_SEND=true` in
  the backend's environment. This is a deliberate, hard-coded safety
  default — there is no UI toggle for it.
- `GMAIL_ADDRESS` / `GMAIL_APP_PASSWORD` are still required for a live
  send to succeed on top of that.

## 6. Known limitations

- **204 No Content endpoints.** `DELETE /api/campaigns/{id}`,
  `DELETE /api/prospects/{id}`, and `DELETE /api/settings/suppressions/{email}`
  return `Response(status_code=204)` with `response_model=None` explicitly,
  rather than a bare `-> None` return annotation. On some FastAPI/Pydantic
  version combinations, a `-> None` annotation with no explicit
  `response_model` gets resolved into a real (truthy) `NoneType` response
  model, which fails FastAPI's own assertion that a 204 response must not
  have a body -- and that assertion runs at **route registration / import
  time**, so it crashes the entire app on startup, not just that one
  request. If you add a new 204 endpoint, follow the same pattern:
  `@router.delete(..., status_code=204, response_model=None)` returning a
  `fastapi.Response` instance, not `None`.

- **Job history is in-memory only.** Restarting the backend process loses
  the list of past jobs (not campaign/lead/email data — that's all in
  SQLite). A production deployment would move `app/workers/jobs.py`'s
  state into a persistent store (or swap it for Celery/RQ) without
  changing any service-layer call sites.
- **Discovery progress is coarse.** `orchestrator.run_discovery()` is a
  single blocking call with no internal progress hooks (search + scrape +
  LLM classify + qualify all happen inside it) — the discovery job reports
  one coarse phase rather than fabricating a fake percentage. Every
  downstream stage (qualification/email-discovery/validation/generation/
  sending) loops per-lead in the job worker itself and reports real,
  granular progress.
- **Single-process, single-SQLite-file deployment.** Matches the existing
  project's design (see `lead_store.py`'s own docstring) — no Postgres/
  Redis required for local use. Concurrent writers are serialized with an
  in-process lock; this is not intended for multi-machine deployment
  as-is.
- **Prospect delete endpoint is a stub.** `DELETE /api/prospects/{id}`
  returns 204/404 correctly but does not hard-delete the underlying Lead
  row — `LeadStore` intentionally has no delete method (leads are the
  audit trail behind email sends), so a real "remove from campaign"
  feature needs an explicit product decision (soft-delete flag? separate
  table?) before it's implemented.
- **The one pre-existing test failure** (`test_parse_angel_hit` in
  `scripts/pipeline/test_quality_smoke.py`) is unrelated to this work and
  was present before any backend/frontend code was added — see the final
  report.
