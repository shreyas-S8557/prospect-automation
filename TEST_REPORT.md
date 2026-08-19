# Day 10 Final Test Report

## Environment

- OS: Linux (container), x86_64
- Python: 3.12.3
- Test runner: `unittest` (stdlib) — `pytest` was not available and could
  not be installed in this environment (no network access for `pip
  install`); every test file in this project is written in
  `unittest.TestCase` style, so results are equivalent either way.
- External services reachable from this environment: **none** (network
  access is disabled). All results below are from a fully offline run.

## Test breakdown

| Suite | File | Tests | Passed | Failed | Errors | Purpose |
|---|---|---:|---:|---:|---:|---|
| Day 4 | `test_lead_pipeline_day4.py` | 33 | 33 | 0 | 0 | InvestorRow→Lead normalization, dedup identity, qualify/filter |
| Day 5 | `test_email_discovery_day5.py` | 50 | 50 | 0 | 0 | Candidate generation, dedupe, MX checking, scoring/ranking |
| Day 6 | `test_email_validation_day6.py` | 44 | 44 | 0 | 0 | Candidate selection, validation status transitions |
| Day 7 | `test_email_generation_day7.py` | 69 | 69 | 0 | 0 | Template rendering, EmailJob persistence, approve/reject |
| Day 8 | `test_email_sending_day8.py` | 55 | 55 | 0 | 0 | Queueing, Gmail send (fake SMTP), retries, stuck-SENDING recovery |
| Day 9 | `test_operational_controls_day9.py` | 54 | 54 | 0 | 0 | Pause/resume/stop, limits/delay/retry, suppression, duplicate blocking |
| **Day 4–9 subtotal** | | **305** | **305** | **0** | **0** | Matches the previously reported 305/0/0 — confirmed unchanged. |
| Day 10 | `test_e2e_day10.py` (new) | 2 | 2 | 0 | 0 | Real, deterministic 5-lead end-to-end run + restart/no-resend safety |
| Smoke (pre-existing, not part of the 305) | `test_qualification_smoke.py`, `test_quality_smoke.py`, `test_target_config_smoke.py` | 3 files | 0 | 0 | 3 | Import-time `ModuleNotFoundError` for `openai`/`httpx` — see Known Limitations |
| **Grand total** | | **310 collected** | **307** | **0** | **3** | |

Zero failures. Zero test-logic errors. The 3 errors are import-time
`ModuleNotFoundError`s for optional third-party packages (`openai`,
`httpx`) that could not be installed in this sandboxed environment (no
network access) — not defects in the pipeline code itself. See "Known
limitations."

## End-to-end integration testing (Day 10, item 3)

A new deterministic end-to-end test (`test_e2e_day10.py`) drives 5
synthetic/fictional leads (no real people) through every real production
function in the pipeline, in-memory SQLite, with only external network
calls mocked:

```
TargetConfig -> normalize_investor_row -> LeadStore (DISCOVERED)
  -> qualify_lead                          (QUALIFIED / FILTERED_OUT)
  -> generate_candidates_for_lead          (EMAIL_CANDIDATES_FOUND / EMAIL_NOT_FOUND)
  -> validate_and_select_email             (EMAIL_VALIDATED / VALIDATION_FAILED)
  -> create_campaign + generate_email_for_lead (EMAIL_GENERATED)
  -> approve_email                         (APPROVED)
  -> queue_approved_email                  (QUEUED)
  -> send_queued_email -> GmailSender       (SENDING -> SENT)
```

Result: of 5 synthetic leads, 1 was correctly `FILTERED_OUT` (didn't match
target criteria), 1 was correctly routed to `EMAIL_NOT_FOUND` (no
name/company to generate a candidate from), and the remaining 3 travelled
all the way to `SENT`. `get_campaign_stats()` was checked against the
exact expected funnel counts. A second test confirms that re-invoking
`send_queued_email` against an already-`SENT` lead_id raises `NotQueued`
rather than re-sending — restart safety, checked directly against the
`FakeSMTPClient` call count (no crash-mid-send scenario, which is already
covered by Day 8's stuck-SENDING tests, was re-tested here).

**Integration boundaries exercised, per hand-off:**

| Boundary | Status | Notes |
|---|---|---|
| Discovery → Qualification | Tested (mocked discovery input) | Real `qualify_lead`/`matches_target_criteria` |
| Qualification → LeadStore | Tested | Real SQLite writes, verified via `list_by_status` |
| LeadStore → Email discovery | Tested (offline generator) | `ScrapegraphPatternGenerator` + `NullMXChecker`, no network |
| Email discovery → Validation | Tested | Real `validate_and_select_email` |
| Validation → Campaign | Tested | Real `create_campaign`/template rendering |
| Campaign → Generation → Approval → Queue | Tested | Real functions throughout |
| Queue → Gmail | Tested with **fake** SMTP client | `FakeSMTPClient` (reused from Day 8 suite) — records calls, never opens a socket |
| Gmail → SENT/FAILED | Tested (fake) | Real state-transition + `EmailSend` bookkeeping code path |

## External services: what was mocked vs. real

| Service | Real or Mocked in this test run | Detail |
|---|---|---|
| Gmail SMTP | **Mocked** | `FakeSMTPClient` injected via `GmailSender(smtp_client_factory=...)` |
| MX/SMTP DNS checking | **Mocked** (`NullMXChecker`) | No network access in this environment; `NodeMXChecker`/`NodeSMTPChecker` exist and are covered by their own Day 5 unit tests but were not exercised against real DNS here |
| DuckDuckGo / Exa / webclaw / agentcrawl / crawl4ai discovery sources | **Not exercised** | Require network + `openai`/`httpx`, unavailable in this sandbox; each has its own unit tests under Day 5 that mock the HTTP layer |
| LLM classification (`llm.py`) | **Not exercised** | Requires `OPENAI_API_KEY` + network |

### Gmail — explicit statement

**Gmail authentication/send was not verified against a real account.** No
real Gmail credentials or network access were available in this
environment. The send path (queueing, retry, stuck-row recovery, and the
exact `smtplib`-shaped call sequence `login()`/`sendmail()`/`quit()`) is
fully covered by 55 Day-8 unit tests plus the new Day-10 end-to-end test,
all against a fake SMTP client. Before running a real campaign, follow the
"Safe test-send procedure" in `HOW_TO_USE.md` against an address you own.

## Known limitations

- **`openai`/`httpx` not installable in this build environment** (no
  network access for `pip install`), so `test_qualification_smoke.py`,
  `test_quality_smoke.py`, and `test_target_config_smoke.py` error at
  import time. These three files aren't part of the reported 305 Day 4–9
  count and don't affect the core lead/campaign/send pipeline — they
  exercise the CLI orchestrator and `ddgs` discovery source, which are
  optional. Install `openai httpx` and re-run to confirm.
- **`pytest` was not installed**; all tests were run with stdlib
  `unittest`, which every test file already targets. No `pytest`-specific
  syntax is used anywhere in the suite, so this is not expected to change
  results if `pytest` is later installed.
- **Real Gmail send is unverified** (see above) — mocked-only in this
  environment.
- **Real MX/SMTP DNS checking (`NodeMXChecker`/`NodeSMTPChecker`) is
  unverified against live DNS** in this environment (no network); unit
  tests mock the Node subprocess call.
- **Live discovery sources (`ddgs`, Exa, webclaw, agentcrawl, crawl4ai)
  were not exercised live** — no network access. Each has its own mocked
  unit-test coverage under Day 5.
- **`Mailfoguess`/`email-finder-main` vendor integration**: `email_discovery.py`
  soft-imports these as optional generators (`MailfoguessGenerator`,
  `EmailFinderMainGenerator`) and degrades gracefully to the built-in
  `ScrapegraphPatternGenerator` if the vendor scripts aren't present —
  this graceful-degradation path is unit-tested; the vendor scripts
  themselves (present under `Email_Finder/` in this checkout) were not
  independently re-verified in Day 10.
- **Age data**: `age`/`age_source`/`age_confidence` are only ever populated
  as an explicitly labelled proxy (e.g. from a stated graduation year) —
  never inferred as fact. This is enforced by `quality.py` and covered by
  existing tests; not re-verified further in Day 10.
