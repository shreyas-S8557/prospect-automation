# Prospect Automation Pipeline

> **New:** this project now also has a FastAPI backend + a simple web UI on
> top of the pipeline described below — see [`DEVELOPMENT.md`](DEVELOPMENT.md)
> to run it, and [`API.md`](API.md) for the REST API reference. Everything
> in this file still works exactly as-is via the CLI; the app is an
> optional layer on top, not a replacement.

A configurable prospect-discovery pipeline: find LinkedIn profiles matching a
`TargetConfig` (titles/industries/keywords/locations/age/company size),
qualify them with evidence-based checks, find and validate a likely email
address, then draft and send outreach — all driven from one JSON config,
no code changes needed to run a different campaign (SaaS founders,
CPA partners, Fintech/blockchain founders, ...).

(The previous, more generic project README is preserved at
`README.old.md`; this one reflects the current, campaign-tested state and
the specific gotchas found running it for real.)

## 1. Setup

```powershell
pip install pyyaml python-dotenv ddgs openai httpx tqdm
$env:OPENAI_API_KEY="sk-..."
```

MX/domain checking (`email_discovery`) also needs **Node.js** on PATH —
check with `node --version`. Without it, MX checks degrade to "unknown"
(never silently read as confirmed-dead) and email ranking falls back to a
`.com`-first guess.

To actually send email (last step only), you also need:
```powershell
$env:GMAIL_ADDRESS="you@gmail.com"
$env:GMAIL_APP_PASSWORD="xxxx xxxx xxxx xxxx"   # Google Account -> Security -> App Passwords, NOT your normal password
```

## 2. Configure a campaign

Edit or copy a file under `data/target_configs/`, e.g. `saas_founders.json`:

```json
{
  "name": "saas_ai_founders",
  "titles": ["Founder", "CEO", "CTO"],
  "industries": ["SaaS"],
  "keywords": ["AI", "automation"],
  "locations": ["United States"],
  "target_count": 50,
  "age_min": 22,
  "age_max": 35,
  "exclude_keywords": []
}
```
Every discovery query, qualification check, and rejection reason is
derived from this file — nothing SaaS/AI/CPA-specific is hard-coded in the
Python.

## 3. Run the pipeline

```powershell
cd scripts

# 1. Discover + qualify (writes data/<name>.csv and data/<name>_qualified.csv)
python -m pipeline.orchestrator --config ../data/target_configs/saas_founders.json --phases ddgs,classify,company_name,age,qualify

# 2. Load qualified leads into the LeadStore
python ingest_csv_to_leadstore.py --csv ../data/saas_ai_founders_qualified.csv --campaign-id saas_ai_founders --config ../data/target_configs/saas_founders.json

# 3. Find likely email addresses
python -m pipeline.email_discovery --campaign-id saas_ai_founders

# 4. Validate them (MX check, optional SMTP)
python -m pipeline.email_validation --campaign-id saas_ai_founders
```

**Why `--phases ddgs,classify,company_name,age,qualify`:** the full default
phase list also includes `fund_flow` (a hard-coded angel-investor CSV),
`http_seeds`/`webclaw`/`agentcrawl`/`crawl4ai` (accounting-firm directory
scrapers) — all left over from this project's original CPA-partner use
case. They're harmless (a source failing just prints a warning and the run
continues), but for a non-CPA/non-investor campaign they only add noise:
`fund_flow` alone added 17 angel investors to one real 50-lead SaaS run,
all correctly disqualified but wasting a third of the target count on
profiles that could never match. Leave them out unless you're actually
after CPA firms or angel investors.

## 4. Send outreach

Nothing above drafts or sends anything — that's a separate, explicit stage.

```powershell
# 5. Create the campaign's subject/body templates (one-time per campaign)
python create_campaign.py --campaign-id saas_ai_founders --name "SaaS Founders Outreach" `
  --subject "Quick question about {{company_name}}" `
  --body "Hi {{first_name}}, saw your work at {{company_name}}. Worth a quick chat?" `
  --sender-name "Your Name"

# 6. Render a personalized draft for every EMAIL_VALIDATED lead
python -m pipeline.email_generation --campaign-id saas_ai_founders

# 7. Read the drafts and approve the ones you're happy with
python review_and_approve_emails.py --campaign-id saas_ai_founders --preview-only
python review_and_approve_emails.py --campaign-id saas_ai_founders --approve-all

# 8. Send (only APPROVED leads go out; --max-per-run/--delay-seconds pace it)
python -m pipeline.email_sending --campaign-id saas_ai_founders --max-per-run 50 --delay-seconds 2.0
```

Supported `{{variables}}` in subject/body: `first_name`, `last_name`,
`company_name`, `job_title`, `location`, `industry`.

Useful extras:
```powershell
python -m pipeline.email_sending --campaign-id saas_ai_founders --test-email you@gmail.com   # send yourself one test first
python -m pipeline.email_sending --campaign-id saas_ai_founders --stats                       # progress so far
python -m pipeline.email_sending --campaign-id saas_ai_founders --pause                       # pause / --resume / --stop
```

Sending is genuinely one-way and rate-limited by Gmail's own anti-abuse
systems — always `--test-email` yourself first and start with a small
`--max-per-run`.

## 5. Scaling up (e.g. 5,000 leads instead of 50)

Set `"target_count": 5000` and be aware of what actually has to scale with
it:

- **Query budget.** `build_queries()` for the SaaS config generates ~4,300
  queries; with roughly 1 accepted lead per 3-5 queries tried, that's
  enough headroom for a few thousand leads before the query list is
  exhausted, but not an unlimited supply — for 5,000+ you may need to add
  more `titles`/`keywords`/locations to the config, since query volume
  scales with the size of those lists (see `query_generator.py`).
- **LLM cost/time.** `classify`, `company_name`, and the LLM half of `age`
  all run once per candidate. A 50-lead run already took ~1-2 minutes per
  phase on the free-tier backend in the logs you shared; expect that to
  scale roughly linearly (100x leads ≈ 100x LLM time/cost), so a 5,000-lead
  run is a multi-hour job. A paid `OPENAI_API_KEY`/faster model would speed
  this up substantially over the free default backend.
- **Age web-search enrichment is the slowest phase per-lead** — it makes
  1-2 *extra* DDGS searches per still-unresolved candidate. At ~5s/lead
  (per the logs you shared) that's ~7 hours for 5,000 leads. If age isn't
  critical for a large run, drop `age` from `--phases` or expect it to
  dominate total runtime.
- **Rate limiting/timeouts.** You're already seeing occasional
  `ConnectTimeout` warnings from `ddgs` at just 50 leads — these are
  handled gracefully (skipped, not fatal), but they'll be more frequent at
  higher query volume. `sources/ddgs_search.py`'s `DISCOVERY_DELAY_SEC`
  (currently 0.25s between queries) is the main throttle if you need to
  slow down further.
- **Resumability already exists for this.** The dedup-memory system
  (`--config` runs reuse `data/<name>.csv` as `in-memory` count on the next
  run, as seen in your logs: `in-memory=22` / `dedup-blocked=0`) means a
  large run can be safely re-launched after an interruption instead of
  starting over — already-discovered LinkedIn slugs aren't re-fetched.

In short: 5,000 is workable, but plan for a long-running, resumable job
rather than one command that finishes in minutes, and consider trimming
`age` or paying for faster LLM access if turnaround time matters.

## 6. Testing

No network or API key needed:
```powershell
cd scripts
python -m pipeline.test_discovery_contamination_day11
python -m pipeline.test_email_discovery_day5
python -m pipeline.test_email_validation_day6
python -m pipeline.test_email_generation_day7
python -m pipeline.test_email_sending_day8
python -m pipeline.test_qualification_smoke
python -m pipeline.test_target_config_smoke
python -m pipeline.test_lead_pipeline_day4
python -m pipeline.test_operational_controls_day9
python -m pipeline.test_e2e_day10
```

## 7. Known limitations

- **Free-tier LLM backend.** By default this project talks to a free,
  unauthenticated proxy (`FreeLLMAPI`), not a paid OpenAI key — it's
  noticeably slower and less reliable than a real API key. Set
  `OPENAI_BASE_URL`/`OPENAI_API_KEY` to point at your own account for
  better results.
- **Age evidence is inherently sparse.** Most people's public web presence
  never states an age or graduation year; even with web-search enrichment,
  expect a meaningful fraction of qualified leads to have no usable age
  data. The system never guesses — a blank is the honest answer, not a bug.
- **Company/email guessing is probabilistic, not perfect.** Company
  extraction depends on the LinkedIn snippet actually stating an employer;
  email guessing tries common name/domain patterns (now across `.com`,
  `.ai`, `.io`, `.co`, not just `.com`) and validates what it can via MX,
  but a correct guess is never guaranteed — treat generated emails as
  candidates to verify, not confirmed addresses.
- **`fund_flow`/`webclaw`/`agentcrawl`/`crawl4ai` are CPA-era leftovers.**
  They still work but aren't config-aware in the same way `ddgs` is —
  exclude them via `--phases` for non-CPA campaigns (see section 3).
