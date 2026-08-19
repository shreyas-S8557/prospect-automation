# Prospect Automation

A lead-generation and email-outreach pipeline: discover prospects against a
configurable target profile, qualify them, find and validate an email
address, generate a personalized email from a campaign template, route it
through review/approval, and send it via Gmail — with pause/resume/stop
controls, per-run sending limits, retries, a do-not-contact suppression
list, and duplicate-send protection, all backed by a crash-safe SQLite
store.

```
TargetConfig -> Discovery -> Qualification -> LeadStore
  -> Email Discovery -> Email Validation
  -> Campaign -> Email Generation -> Approval -> Queue -> Gmail
  -> Campaign Controls / Statistics
```

## Start here

- **`HOW_TO_USE.md`** — installation, configuration (including Gmail App
  Password setup), and how to run every stage of the pipeline.
- **`ARCHITECTURE.md`** — module-by-module technical overview and the
  pipeline's state machine.
- **`TEST_REPORT.md`** — exact test results, and what was tested against
  real external services vs. mocked/faked ones.
- **`FINAL_STATUS.md`** — feature-by-feature status and what remains
  before this should be pointed at real prospects.

## What's in this package

- `scripts/pipeline/` — the application code and its test suite (310
  tests; see `TEST_REPORT.md`).
- `Email_Finder/` — the vendored pattern-based email-candidate generator
  and Node-based MX/SMTP checker scripts that `pipeline/email_discovery.py`
  calls into.
- `data/target_configs/`, `data/*_seeds.yaml`, `data/us_cities.txt` —
  small config/seed files used by discovery and target configuration.
  **No lead data is included** — this package ships with an empty slate;
  running discovery against your own `TargetConfig` populates your own
  SQLite database.
- `.env.example` (root and `scripts/`) — every environment variable the
  pipeline reads, with placeholder values.

This package was extracted from a larger development checkout for a
clean, self-contained handoff — see `FINAL_STATUS.md` for what was
intentionally left out (a large real scraped-lead dataset from earlier
development, kept only in the original working directory) and why.

## Responsible use

This project can generate and send real email to real people once you
configure Gmail credentials and approve a campaign. Before running it
against anyone who hasn't asked to hear from you:

- Make sure your outreach complies with applicable anti-spam law for your
  recipients (e.g. CAN-SPAM, CASL, GDPR/ePrivacy) — this is a legal
  question outside the scope of this codebase, and the suppression list
  here is a mechanism, not a substitute for that judgment call.
- Give recipients a real way to opt out, and honor it via
  `suppression.suppress_email()`.
- Use the safe test-send procedure in `HOW_TO_USE.md` before pointing a
  campaign at anyone.
