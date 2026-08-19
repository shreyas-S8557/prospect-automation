# Discovery-layer contamination fix — report

## Sandbox limitation (read first)

This build environment has **no network access** (all egress is denied)
and the `ddgs`/`openai`/`httpx` packages cannot be installed here — the
same limitation already documented in `FINAL_STATUS.md`/`TEST_REPORT.md`
from the prior round. That means:

- I could not run a fresh **live** DDGS search to populate a brand-new
  50-record file.
- Instead I replayed the exact raw `(title, snippet, linkedin_url)` data
  already captured in the existing 50-record file — snapshotted, untouched,
  at `data/saas_ai_founders_raw_stale_snapshot.csv` — through the fixed
  parser and qualifier. This is the same input data your original bug
  report was based on (it contains the literal "Austin Maxwell / Phillip
  Zedalis / McFadyen Anderson" example), so it's a faithful way to prove
  the fix changes the outcome on real, previously-observed noisy data.
- I also could not exercise the **LLM half** of per-candidate enrichment
  (`extract_company_names_llm`, `extract_ages_llm`, task item 6) — that
  needs `OPENAI_API_KEY` + network. Only the free, deterministic half
  (`extract_company_from_snippet`, `extract_age_proxy`) ran. In a
  networked run, the `company_name`/`age`/`qualify` phases would research
  thin candidates further, which should raise the qualified count above
  what's reported here.
- To make the existing `openai`/`httpx`/`tqdm`/`ddgs`-dependent modules
  even *importable* for testing in this sandbox, I added throwaway stub
  packages under a separate `/home/claude/test_stubs` directory (not part
  of this zip) that raise on any actual network call. This let me run
  files that previously errored at import (`test_qualification_smoke.py`,
  `test_quality_smoke.py`, `test_target_config_smoke.py`) for the first
  time and see their actual logic results, not just import errors.

I'm flagging this prominently because you explicitly asked me not to
claim the problem is fixed without running and inspecting the test —
what I ran is a faithful replay of real captured data, not a live search,
and the numbers below reflect that.

## 1. What was actually broken

The decontamination system in `quality.py` (evidence-scoped
`build_evidence_text`, `_looks_multi_person`, `qualify_row`, etc.) already
existed from a prior round, but three concrete gaps let contamination
reach the output CSV anyway:

1. **`sources/ddgs_search.py` never rejected a contaminated hit outright.**
   It built a candidate straight from the raw glued title/summary
   (`"Austin Maxwell - Cofounder - Kanga Coolers | Shark Tank
   ...Phillip Zedalis - Chief AI Officer & Co-Founder | LinkedInMcFadyen
   Anderson..."`) and relied entirely on downstream evidence-blanking. That
   still let a wrong **location** slip through — `extract_location()` was
   called on the raw, un-decontaminated blob, so a candidate could end up
   with a location that actually belonged to a different glued-in person.

2. **`_titles_ok()` matched against the raw, contaminated
   `profile_title`**, not the decontaminated version — so another person's
   job title glued into the same field could satisfy the `titles`
   dimension for the wrong person.

3. **The `_looks_multi_person()` contamination heuristic was too narrow**
   (only `"LinkedIn"` count ≥2 or `"Experience:"` count ≥2). It happened to
   catch the exact bug-report example, but missed other real glued
   patterns present in your own data (repeated `"Name - Title"` headers,
   repeated `"View X's profile on LinkedIn"` sentences).

4. **Separately (task item 8): the LLM fallback paths in `llm.py` were
   hard-coded to a "US CPA firm partner" prompt**, regardless of which
   campaign was actually running. `parse_search_hit_llm` (the LLM rescue
   path inside the DDGS discovery loop itself), `classify_industries_llm`
   (runs by default in every campaign's `"classify"` phase), and
   `extract_investors_from_markdown` (directory-harvest phase) all ignored
   the active `TargetConfig` entirely. For a SaaS campaign this meant the
   `"classify"` phase would ask an LLM to infer "CPA practice areas" for a
   SaaS founder — a real configurability violation, independent of the
   discovery-contamination bug.

## 2. What I changed

| File | Change |
|---|---|
| `scripts/pipeline/quality.py` | Added `is_contaminated_hit()` (whole-result reject gate) and broadened `_looks_multi_person()` to also catch repeated `"Name - Title"` headline prefixes and repeated `"View X's profile on LinkedIn"` sentences, not just repeated `"LinkedIn"`/`"Experience:"` counts. Fixed `_titles_ok()` to read the decontaminated title, not the raw field. |
| `scripts/pipeline/sources/ddgs_search.py` | `parse_search_hit()` now calls `is_contaminated_hit()` and rejects the whole hit **before** extracting a name, location, or anything else from it — per your explicit "reject instead of extract" requirement. Added an opt-in `require_target_match=False` parameter (defaults to `True`, i.e. unchanged production behaviour) purely so the verification script below can separate "is this a clean single-person candidate" from "does the raw snippet alone also prove SaaS/AI". |
| `scripts/pipeline/query_generator.py` | Added a tighter `keyword × industry × title` query tier (e.g. `"AI SaaS Founder LinkedIn"`), on top of the existing config-driven tiers, to better match the query patterns in your example list. Still entirely built from whatever the `TargetConfig` specifies — nothing industry-specific is hard-coded. |
| `scripts/pipeline/llm.py` | Added `_person_description()`, built from the active `TargetConfig`. `parse_search_hit_llm`, `classify_industries_llm`, and `extract_investors_from_markdown` now build their prompts from it instead of a hard-coded CPA description, and `parse_search_hit_llm` also runs the same `is_contaminated_hit()` gate before ever asking the LLM to "pick out" a person from ambiguous glued text. |
| `scripts/pipeline/test_discovery_contamination_day11.py` (new) | Locks in the fix: rejects the exact bug-report example, still accepts a clean single-person hit, confirms `_titles_ok` ignores contaminated titles, confirms query generation stays config-driven across two different ICPs (SaaS+AI vs. Fintech+blockchain) with no cross-contamination of hard-coded terms. |
| `scripts/replay_saas_test.py` (new) | The replay/verification tool described above. Regenerates `data/saas_ai_founders.csv`. |

## 3. 50-record test — results

Command used (replay, see limitation note above for why not live):
```
python3 scripts/replay_saas_test.py
```
(the equivalent live command, in a networked environment with
`OPENAI_API_KEY` set, is:
`python3 -m scripts.pipeline.orchestrator --config data/target_configs/saas_founders.json`)

| Metric | Count |
|---|---:|
| Input raw rows (previous stale output) | 50 |
| **Discovered candidates** (single clean person; passed the contamination/name/location/company-page gate) | **22** |
| **Rejected at discovery** (contaminated, not-a-person, or no usable name/location) | **28** |
| **Qualified** (passed `qualify_row` against `saas_founders.json`) | **0** |
| Disqualified (clean candidate, but failed target criteria) | 22 |
| With confirmed keyword relevance (AI/automation evidence) | 0 |
| With confirmed industry evidence (SaaS) | 0 |
| With usable age evidence | 0 |

Full report with example rejection/disqualification reasons for every
category: `data/saas_ai_founders_replay_report.txt`.

**Why qualified = 0:** literally 2 of the 50 raw snippets contain the word
"SaaS" anywhere at all, and both of those 2 are themselves contaminated
(rejected at discovery). LinkedIn headline/snippet text essentially never
states "SaaS" outright — real qualification for a narrow single-word
industry like this depends on the LLM company-research enrichment step
(task item 6), which needs `OPENAI_API_KEY` + network, unavailable here.
This is a genuine, expected consequence of the sandbox limitation, not a
regression — every one of the 22 clean candidates gets a specific,
inspectable `qualification_reason` (e.g. *"no strong evidence of required
industry (SaaS)..."*), not a bare rejection.

**What the 28 rejections prove:** every one of the multi-profile
contamination cases from your bug report is now caught, including the
exact "Austin Maxwell / Phillip Zedalis / McFadyen Anderson" example and
similar ones your own data contained (`Chuck Foster`/`James Green`,
`Bryan Landerman`/`Brian Wehrle`, `Charlie Banks`/`Alex Skatell`, `Daniel
Smith`/`Ejay O'Donnell`, `Erin Cornell`/`Ashley Murphy`, `Justin
Nevins`/`Scot Chisholm`, and others) — see
`data/saas_ai_founders_replay_report.txt` for the full list with the
exact glued title text that triggered rejection.

## 4. Test suite

Run with (stdlib `unittest`, same as the existing convention in this
repo; `pytest` isn't installed here either):

```
cd scripts && python -m pipeline.test_<name>
```

| Suite | Result |
|---|---|
| `test_lead_pipeline_day4` | 33/33 passed |
| `test_email_discovery_day5` | 50/50 passed |
| `test_email_validation_day6` | 44/44 passed |
| `test_email_generation_day7` | 69/69 passed |
| `test_email_sending_day8` | 55/55 passed |
| `test_operational_controls_day9` | 54/54 passed |
| `test_e2e_day10` | 2/2 passed |
| `test_qualification_smoke` | all checks passed |
| `test_target_config_smoke` | all checks passed |
| `test_discovery_contamination_day11` (new) | all 12 checks passed |
| `test_quality_smoke` | **1 pre-existing failure, see below** |

**`test_quality_smoke.py::test_parse_angel_hit` fails, but not because of
this fix.** This file (along with `test_qualification_smoke.py` and
`test_target_config_smoke.py`) has always errored at *import* time in
every sandboxed run to date (no `openai`/`httpx` installed — see
`TEST_REPORT.md`'s "3 errors"), so its actual assertions had never
actually been exercised before. With stub packages in place to get past
the import, one of its three tests turns out to fail: it calls
`parse_search_hit(hit)` with **no `target=` argument** on an angel-investor
fixture, which falls back to the process-wide default target — the
`CPA_PARTNER_PRESET` — which correctly requires CPA-firm evidence and so
correctly rejects an angel-investor snippet. I verified directly that this
is unrelated to my contamination fix (the same rejection happens with
`is_contaminated_hit()` bypassed entirely) and is a pre-existing
test/default-target mismatch, not something introduced by this change. I
left it alone since it's outside the scope of the SaaS discovery fix you
asked for, but flagging it since I'm now able to see it fail for the first
time.

## 5. Files in `data/`

- `saas_ai_founders_raw_stale_snapshot.csv` — untouched snapshot of the
  original noisy 50-record output you reported the bug against (kept for
  reproducibility).
- `saas_ai_founders.csv` — regenerated: the 22 structurally clean,
  single-person candidates from that raw data, each with a
  `qualification_status`/`qualification_reason`.
- `saas_ai_founders_qualified.csv` — the subset that fully qualified (0,
  for the enrichment-limitation reason above).
- `saas_ai_founders_replay_report.txt` — full report incl. every
  rejection/disqualification reason.

## 6. Not sent

No emails were sent or attempted at any point.
