# How to Use

This guide covers installing, configuring, and running the prospect
automation pipeline (`scripts/pipeline/`). This package contains only the
application code, tests, and minimal supporting config for that pipeline
— it was extracted from a larger development checkout (which also
contained an unrelated third-party scraping library and a large real
scraped-lead dataset) for a clean, self-contained handoff. See
`FINAL_STATUS.md` for what was intentionally left out and why.

## Prerequisites

- **Python**: 3.10+ (developed and tested on 3.12).
- **Node/Go**: not required. `email_discovery.py`'s `NodeMXChecker` /
  `NodeSMTPChecker` shell out to a local Node.js script for real MX/SMTP
  checks if you enable them, but the pipeline runs fine with the
  `Null*Checker` no-op variants if Node isn't installed (see "Optional:
  MX/SMTP checking" below).
- **External services** (all optional except Gmail, which is only needed
  once you actually want to send):
  - A Gmail account with an **App Password** (see below) — required only
    for the `email_sending`/`gmail_sender` stage.
  - An OpenAI-compatible LLM endpoint (`OPENAI_API_KEY`) — only needed by
    `llm.py` (industry classification / company-name extraction) and the
    `ddgs` discovery source.
  - Exa API key — only needed if you run discovery with `--use-exa`.

## Installation

```bash
# 1. Extract the archive
unzip prospect-automation-final.zip
cd prospect-automation

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 3. Install the minimum needed to run the lead/campaign/send pipeline
pip install pyyaml python-dotenv

# 4. (Optional) install the extra packages needed for live discovery
pip install ddgs openai httpx tqdm "agentcrawl-ai[browser]" crawl4ai
playwright install chromium   # only if you installed agentcrawl-ai/crawl4ai
```

There is no build step — this is a pure-Python project, no compiled
extensions.

## Configuration

Copy the template and fill in only what you need:

```bash
cp .env.example .env
```

| Variable | Required for | Notes |
|---|---|---|
| `GMAIL_ADDRESS` | Sending emails | The Gmail address you're sending from. |
| `GMAIL_APP_PASSWORD` | Sending emails | 16-character **App Password**, not your normal Gmail password (see below). |
| `OPENAI_API_KEY` | LLM classification, `ddgs` discovery | Any OpenAI-compatible key. |
| `OPENAI_BASE_URL` | Only if not using api.openai.com | Defaults to a pre-set endpoint in `config.py` if omitted — override this for your own provider. |
| `LLM_MODEL` | LLM classification | Defaults to `"auto"` if unset. |
| `EXA_API_KEY` | `--use-exa` discovery only | Not required for the core pipeline. |
| `WEBCLAW_API_KEY` | `webclaw` discovery source only | The webclaw HTTP-BFS fallback works without it. |

### Gmail App Password setup

Gmail will reject a normal account password over SMTP once 2-Step
Verification is on (and Google recommends 2-Step Verification generally).
To get an App Password:

1. Turn on 2-Step Verification: Google Account → Security → 2-Step
   Verification.
2. Google Account → Security → App passwords → create one for "Mail".
3. Copy the 16-character password into `GMAIL_APP_PASSWORD` (no spaces).

### Credential handling / security

- Real credentials belong only in `.env` (git-ignored) — never in code,
  never in `.env.example`, never in a committed config file.
- `GmailSender` reads credentials **only** from `GMAIL_ADDRESS` /
  `GMAIL_APP_PASSWORD` (env vars, or explicit constructor args in tests) —
  it never hard-codes or logs them.
- Before distributing or committing this project, grep for accidental
  secrets: `grep -rn "APP_PASSWORD\|API_KEY" --include=*.py .` and confirm
  every hit is reading from `os.environ`/`os.getenv`, not a literal value.

## Running the pipeline end to end

### 1. Define a target configuration

`TargetConfig` (`scripts/pipeline/target_config.py`) drives discovery and
qualification:

```python
from pipeline.target_config import TargetConfig

target = TargetConfig(
    locations=["United States"],       # e.g. ["San Francisco Bay Area"]
    titles=["Managing Partner"],       # job-title keywords
    industries=["Fintech", "SaaS"],    # industry keywords
    keywords=["seed stage"],           # free-text positive signal words
    exclude_keywords=["student"],      # free-text negative signal words
    company_size_min=None,             # optional int bounds
    company_size_max=None,
    age_min=None,                      # optional; age is only ever used as an
    age_max=None,                      # explicitly-labelled *proxy*, never inferred as fact
    target_count=500,
    name="my-campaign",
)
```

Every field is optional except `target_count`; an empty list means "no
constraint on this dimension," not "match nothing."

### 2. Discover prospects

Run a source directly (see `scripts/collect_us_angel_investors_1000.py` for
a worked CLI example), or call a `sources/*.py` adapter (e.g.
`run_ddgs_phase`) to get `InvestorRow` dicts.

### 3. Ingest + qualify

```python
from pipeline.lead_store import LeadStore
from pipeline.lead_pipeline import ingest_discovery_rows, qualify_pending_leads

store = LeadStore("data/pipeline.db")
ingest_discovery_rows(store, discovered_rows, campaign_id="my-campaign")
qualify_pending_leads(store, campaign_id="my-campaign", target=target)
```

### 4. Find + validate emails

```python
from pipeline.models import PipelineStatus
from pipeline.email_discovery import process_lead_email
from pipeline.email_validation import find_and_validate_pending_leads

for lead in store.list_by_status(PipelineStatus.QUALIFIED, campaign_id="my-campaign"):
    process_lead_email(store, lead)   # defaults use NodeMXChecker if Node is present

find_and_validate_pending_leads(store, campaign_id="my-campaign")
```

#### Optional: MX/SMTP checking

By default `process_lead_email` uses `NodeMXChecker`/`NodeSMTPChecker`,
which shell out to Node for real DNS MX lookups (and, if `enable_smtp=True`,
a real SMTP handshake). If Node isn't available, or you want fully offline
runs, pass `mx_checker=NullMXChecker()` explicitly — every candidate is
then marked `MX_UNKNOWN` rather than failing.

### 5. Create a campaign and generate emails

```python
from pipeline.campaign import create_campaign, save_campaign
from pipeline.email_generation import generate_email_for_lead

campaign = create_campaign(
    name="My Campaign",
    subject_template="Quick question, {{first_name}}",
    body_template="Hi {{first_name}}, ... {{company_name}} ...",
    sender_name="Your Name",
)
save_campaign(store, campaign)

for lead in store.list_by_status(PipelineStatus.EMAIL_VALIDATED, campaign_id="my-campaign"):
    generate_email_for_lead(store, lead, campaign)
```

Only six variables are allowed in templates: `first_name`, `last_name`,
`company_name`, `job_title`, `location`, `industry`. Anything else raises
`UnsupportedTemplateVariable` at campaign-creation time.

### 6. Review, approve, queue

```python
from pipeline.email_generation import approve_email
from pipeline.email_sending import queue_approved_email

for lead in store.list_by_status(PipelineStatus.EMAIL_GENERATED, campaign_id="my-campaign"):
    approve_email(store, lead.lead_id)   # or reject_email(store, lead.lead_id, reason="...")

for lead in store.list_by_status(PipelineStatus.APPROVED, campaign_id="my-campaign"):
    queue_approved_email(store, lead)
```

`queue_approved_email` raises `SuppressedRecipient` if the address is on
the do-not-contact list, and `DuplicateSendBlocked` if that address already
has an in-flight/completed send under a different lead — pass
`allow_duplicate=True` to override the latter deliberately.

### 7. Send through Gmail

```python
from pipeline.config import load_env
load_env()   # loads .env

from pipeline.gmail_sender import GmailSender
from pipeline.email_sending import send_pending_queue

gmail = GmailSender()   # reads GMAIL_ADDRESS / GMAIL_APP_PASSWORD from env
gmail.validate_credentials()   # raises GmailCredentialsError early if missing

send_pending_queue(store, gmail, campaign="my-campaign")
```

#### Safe test-send procedure (do this before any real campaign)

Never point a live campaign at real prospects until you've confirmed
Gmail auth works end to end against an address you control:

```python
from pipeline.gmail_sender import GmailSender
from pipeline.config import load_env

load_env()
gmail = GmailSender()
gmail.validate_credentials()
gmail.connect()
result = gmail.send(
    "your-own-test-address@example.com",   # an address YOU own, never a prospect
    subject="Pipeline test send",
    body="This is a manual test of the Gmail sender.",
)
print(result)
```

**Do not** run this against `send_pending_queue`/a real campaign until
you've verified a single manual send this way. This is the only place a
real email leaves your Gmail account — everything upstream (discovery
through queueing) never talks to Gmail.

> This project ships with the send path fully tested against a **fake**
> SMTP client (see `TEST_REPORT.md`). A real Gmail account was **not**
> available in the environment this Day 10 milestone was built in, so real
> Gmail authentication/sending has not been verified end to end — run the
> procedure above yourself before trusting it against real prospects.

### 8. Pause / resume / stop / view stats

```python
from pipeline.campaign_control import pause_campaign, resume_campaign, stop_campaign, configure_sending
from pipeline.campaign_stats import get_campaign_stats, format_campaign_stats

pause_campaign(store, "my-campaign")
resume_campaign(store, "my-campaign")
stop_campaign(store, "my-campaign")   # terminal: cancels remaining QUEUED sends

configure_sending(store, "my-campaign", max_per_run=50, delay_seconds=2.0,
                  max_retries=2, retry_backoff_seconds=5.0)

print(format_campaign_stats(get_campaign_stats(store, "my-campaign")))
```

## Crash recovery

The pipeline is designed to be safely re-run after an interruption at any
stage:

| Stage interrupted | What happens on restart |
|---|---|
| Discovery / ingest | `ingest_discovery_rows` is idempotent — re-ingesting the same row just updates blank fields, never regresses a lead's status. |
| Qualification | `qualify_pending_leads` only ever pulls leads still in `DISCOVERED`; already-`QUALIFIED`/`FILTERED_OUT` leads are untouched. |
| Email discovery / validation | Same pattern — only `QUALIFIED` (resp. `EMAIL_CANDIDATES_FOUND`) leads are re-picked-up; nothing already past that stage is reprocessed. |
| Generation / approval / queueing | `queue_approved_email` is idempotent against a lead that already has an `EmailSend` row (the crash-between-insert-and-transition race). |
| **Mid-send** | `send_queued_email` writes `QUEUED -> SENDING` *before* the network call. A crash here leaves the row visibly `SENDING`. Use `list_stuck_sending(store, campaign_id=...)` to find these, then either `mark_stuck_as_failed(store, lead_id)` (if you've confirmed via your Gmail Sent folder that it did **not** go out) or `resolve_stuck_as_sent(store, lead_id, ...)` (if it did). `send_pending_queue` never re-picks-up a `SENDING` row on its own — only `QUEUED` — so a restart cannot silently double-send. |

## Suppression & duplicate protection

- `suppress_email(store, email, ...)` adds an address to a do-not-contact
  list; `queue_approved_email` refuses to queue a suppressed address.
- `already_contacted(store, email, exclude_lead_id=...)` blocks a second
  lead record with the same email from being queued unless you pass
  `allow_duplicate=True` to `queue_approved_email`.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ModuleNotFoundError: No module named 'openai'` / `'httpx'` when running the smoke tests or `orchestrator.py`/`ddgs_search.py` | These are optional discovery-source dependencies, not required for the core lead/campaign/send pipeline. `pip install openai httpx` if you need live `ddgs` discovery or the CLI orchestrator; otherwise ignore — 305 of the 310 tests in this project do not require them (see `TEST_REPORT.md`). |
| `ImportError: attempted relative import with no known parent package` when running a test file directly | Run tests as a package from `scripts/`, e.g. `python3 -m unittest pipeline.test_lead_pipeline_day4`, not `python3 pipeline/test_lead_pipeline_day4.py`. |
| `GmailCredentialsError: Missing Gmail credentials` | `GMAIL_ADDRESS`/`GMAIL_APP_PASSWORD` aren't set — check `.env` is present and `load_env()` was called before constructing `GmailSender()`. |
| `SuppressedRecipient` on queue | The address is on the do-not-contact list (expected safety behavior, not a bug). |
| `DuplicateSendBlocked` on queue | Another lead already has a send under this address — pass `allow_duplicate=True` if that's actually intended. |
