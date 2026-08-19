"""FreeLLMAPI client and JSON extraction helpers (CPA firm partner ICP)."""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

from openai import OpenAI

from .config import DEFAULT_BASE_URL, get_active_target
from .models import InvestorRow
from .quality import (
    clean_company_name,
    extract_company_from_at_pattern,
    extract_company_from_snippet,
    extract_email,
    extract_location,
    extract_phone,
    is_contaminated_hit,
    is_non_company_org,
    is_valid_company_name,
    is_valid_person_name,
    normalize_linkedin,
)


def _person_description(target=None) -> str:
    """Plain-language description of who this campaign is looking for,
    built entirely from the active TargetConfig — never hard-coded to any
    one industry/ICP. Falls back to a generic "professional" description
    when a dimension isn't configured, and to the process-wide active
    target (get_active_target(), same default used across the pipeline —
    the CPA preset when nothing else was ever set) when `target` is None,
    so every LLM prompt in this module tracks whatever campaign is
    currently running instead of being frozen to one hard-coded ICP.
    """
    target = target or get_active_target()
    titles = ", ".join(target.titles) if target.titles else "professional"
    bits = [f"a US-based {titles}"]
    if target.industries:
        bits.append(f"in the {', '.join(target.industries)} industry")
    if target.keywords:
        bits.append(f"associated with {', '.join(target.keywords)}")
    if target.locations and target.locations != ["United States"]:
        bits.append(f"located in {', '.join(target.locations)}")
    return " ".join(bits)


def get_llm_client() -> OpenAI:
    return OpenAI(
        api_key=os.getenv("OPENAI_API_KEY"),
        base_url=os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL),
    )


def _llm_model() -> str:
    return os.getenv("LLM_MODEL", "auto")


def extract_json_array(text: str) -> list[Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("no json array in response")
    return json.loads(text[start : end + 1])


def _coerce_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    if isinstance(value, list):
        parts = [_coerce_str(v) for v in value if v is not None]
        return "; ".join(p for p in parts if p) or default
    return str(value).strip()


def extract_json_object(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    if text.lower() == "null":
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


def classify_industries_llm(
    client: OpenAI, batch: list[InvestorRow], target=None
) -> None:
    if not batch:
        return
    target = target or get_active_target()
    payload = [
        {
            "id": i,
            "profile_title": (row.get("profile_title") or "")[:300],
            "summary": (row.get("summary") or "")[:600],
        }
        for i, row in enumerate(batch)
    ]
    focus = ""
    if target.industries or target.keywords:
        dims = ", ".join(target.industries + target.keywords)
        focus = f" Focus particularly on whether/how they relate to: {dims}."
    prompt = (
        f"For each profile below ({_person_description(target)}), infer their "
        "industry/specialization areas in a few words each (e.g. 'AI SaaS', "
        "'Fintech', 'Tax; Audit & Assurance', 'Healthcare Technology')."
        f"{focus} "
        'Return ONLY a JSON array: [{"id":0,"industries":"..."}, ...]. '
        "Semicolon-separated if more than one. Use 'General' if unknown.\n\n"
        + json.dumps(payload)
    )
    try:
        resp = client.chat.completions.create(
            model=_llm_model(),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2000,
            temperature=0,
        )
        data = extract_json_array(resp.choices[0].message.content or "[]")
        for item in data:
            idx = item.get("id")
            if idx is not None and 0 <= idx < len(batch) and not batch[idx].get("industries"):
                batch[idx]["industries"] = item.get("industries", "General")
    except Exception as exc:
        print(f"  llm classify warn: {exc}", file=sys.stderr)
    for row in batch:
        if not row.get("industries"):
            row["industries"] = "General"


def parse_search_hit_llm(
    client: OpenAI,
    *,
    url: str,
    title: str,
    body: str,
    source: str = "ddgs_search",
    target=None,
) -> InvestorRow | None:
    """LLM fallback used only when the deterministic parser
    (sources.ddgs_search.parse_search_hit) couldn't confidently extract a
    candidate. Driven entirely by the active TargetConfig (`target`, or
    get_active_target() when not given) — never hard-coded to any one ICP —
    so this fallback looks for whatever kind of person the campaign is
    actually targeting, not always a CPA partner.

    Contamination guard: this fallback exists to rescue ambiguous-looking
    hits, but a hit that clearly glues multiple LinkedIn profiles together
    (see quality.is_contaminated_hit) is unrecoverable no matter how it's
    parsed — asking the LLM to "extract the one right person" from text that
    mixes several people's names/titles/companies just relocates the risk
    of attributing someone else's data to the target. Those hits are
    rejected before ever reaching the LLM.
    """
    if is_contaminated_hit(title, body):
        return None

    target = target or get_active_target()
    prompt = (
        f"Extract {_person_description(target)} from this search result. "
        "Return ONLY JSON object with keys: name, location, linkedin_url, "
        "profile_title, summary, industries, email, phone. "
        "The result text may only ever describe ONE person — the person at "
        "the given LinkedIn URL. If the title or snippet mixes in names, "
        "titles, or companies belonging to a different person, do not guess "
        "which parts belong to the target: return null JSON instead. "
        "Return null JSON if this is not a valid matching person profile "
        "(e.g. skip a company page, or a profile that doesn't match the "
        "requested criteria).\n\n"
        f"URL: {url}\nTitle: {title}\nSnippet: {body[:1200]}"
    )
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=_llm_model(),
                messages=[{"role": "user", "content": prompt}],
                max_tokens=800,
                temperature=0,
            )
            obj = extract_json_object(resp.choices[0].message.content or "")
            if not obj:
                return None
            li = normalize_linkedin(obj.get("linkedin_url", "") or url)
            if not li:
                return None
            name = _coerce_str(obj.get("name"), title.split("|")[0].split(" - ")[0])
            if not is_valid_person_name(name):
                return None
            loc = _coerce_str(obj.get("location"), extract_location(body, title) or "")
            if not loc:
                return None
            return {
                "name": name,
                "location": loc,
                "linkedin_url": li,
                "profile_title": _coerce_str(obj.get("profile_title"), title)[:500],
                "summary": _coerce_str(obj.get("summary"), body)[:2000],
                "industries": _coerce_str(obj.get("industries")),
                "email": _coerce_str(obj.get("email"), extract_email(body)),
                "phone": _coerce_str(obj.get("phone"), extract_phone(body)),
                "source": source,
            }
        except Exception as exc:
            if attempt == 2:
                print(f"  llm parse warn: {exc}", file=sys.stderr)
            time.sleep(1.0)
    return None


def extract_investors_from_markdown(
    client: OpenAI,
    markdown: str,
    *,
    seed_name: str,
    source: str,
    target=None,
) -> list[InvestorRow]:
    if not markdown.strip():
        return []
    target = target or get_active_target()
    chunk = markdown[:12000]
    prompt = (
        f"Extract people matching {_person_description(target)}, each with a "
        "LinkedIn /in/ profile URL, from this markdown. Each person's data "
        "(name, title, company, etc.) must come only from that person's own "
        "section of the page — never mix in a neighboring person's title or "
        "company. Return ONLY a JSON array. Each item: "
        '{"name","location","linkedin_url","profile_title","summary","industries","email","phone"}. '
        "Skip company pages and non-US profiles. Max 30 per response.\n\n"
        f"Source page: {seed_name}\n\n{chunk}"
    )
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model=_llm_model(),
                messages=[{"role": "user", "content": prompt}],
                max_tokens=4000,
                temperature=0,
            )
            data = extract_json_array(resp.choices[0].message.content or "[]")
            rows: list[InvestorRow] = []
            for item in data:
                li = normalize_linkedin(item.get("linkedin_url", ""))
                name = _coerce_str(item.get("name"))
                if not li or not is_valid_person_name(name):
                    continue
                loc = _coerce_str(
                    item.get("location"),
                    extract_location(_coerce_str(item.get("summary")), name),
                )
                if not loc:
                    continue
                default_title = (target.titles[0] if target.titles else "Professional")
                rows.append({
                    "name": name,
                    "location": loc,
                    "linkedin_url": li,
                    "profile_title": _coerce_str(item.get("profile_title"), default_title)[:500],
                    "summary": _coerce_str(
                        item.get("summary"),
                        f"{default_title} listed on {seed_name}.",
                    )[:2000],
                    "industries": _coerce_str(item.get("industries")),
                    "email": _coerce_str(item.get("email")),
                    "phone": _coerce_str(item.get("phone")),
                    "source": source,
                })
            return rows
        except Exception as exc:
            if attempt == 2:
                print(f"  llm extract warn ({seed_name}): {exc}", file=sys.stderr)
            time.sleep(1.5)
    return []


COMPANY_EXTRACTION_PROMPT = (
    "You are an information extraction model. Your task is to determine the "
    "CURRENT employer/company associated with this person using ONLY the "
    "provided context — never guess or invent one. "
    "If the context distinguishes 'Experience' (current/past employer) from "
    "'Education' (school attended), use ONLY the Experience/employer signal; "
    "NEVER return a school, university, or college the person merely "
    "attended as their company. "
    "Return ONLY the canonical company name. Do not explain your reasoning. "
    "Do not output JSON. Do not output Markdown. Output exactly one company "
    "name, or an empty string if the employer is not clearly stated in the "
    "context."
)

COMPANY_EXTRACTION_STRICT_PROMPT = (
    "You are an information extraction model. Your previous answer was invalid "
    "— either it contained something other than a bare company name, or it "
    "was a school/university/college the person merely attended rather than "
    "their employer. Determine the CURRENT employer/company associated with "
    "this person using ONLY the provided context (never the person's "
    "education/alma mater, never a guess). Respond with NOTHING except the "
    "canonical company name itself: no quotes, no punctuation beyond what's "
    "part of the name, no sentence, no explanation, no JSON, no Markdown, and "
    "never more than one company. If you cannot determine a single company "
    "confidently from stated employer evidence, respond with an empty string "
    "and nothing else."
)


def _company_context(row: InvestorRow) -> str:
    fields = [
        ("Name", row.get("name", "")),
        ("Profile title / headline", row.get("profile_title", "")),
        ("Summary", row.get("summary", "")),
        ("Location", row.get("location", "")),
        ("Industries", row.get("industries", "")),
        ("LinkedIn URL", row.get("linkedin_url", "")),
        ("Source", row.get("source", "")),
    ]
    lines = [f"{label}: {value}" for label, value in fields if value]
    return "\n".join(lines)


def extract_company_name_llm(client: OpenAI, row: InvestorRow) -> str:
    """Infer the employer/company for a single profile via the LLM.

    Uses all available context (title, summary, location, industries, source,
    etc.) and validates the response is a bare canonical company name that
    isn't a university/school/government body (see quality.is_non_company_org
    — those are almost never a genuine employer signal in this pipeline's
    search snippets; they usually leak in from an "Education:" field).
    Retries once with a stricter prompt if the first response is malformed or
    flagged as a non-company org, and falls back to an empty string if
    extraction cannot be confidently completed. Never guesses when there is
    not enough context (title or summary) to extract from.
    """
    if not (row.get("profile_title") or row.get("summary")):
        # LinkedIn URL / name / source alone isn't evidence of an employer.
        return ""

    context = _company_context(row)
    if not context.strip():
        return ""

    system_prompt = COMPANY_EXTRACTION_PROMPT
    for attempt in range(2):
        prompt = f"{system_prompt}\n\nContext:\n{context[:3000]}\n\nCompany name:"
        try:
            resp = client.chat.completions.create(
                model=_llm_model(),
                messages=[{"role": "user", "content": prompt}],
                max_tokens=40,
                temperature=0,
            )
            raw = resp.choices[0].message.content or ""
        except Exception as exc:
            print(f"  llm company warn: {exc}", file=sys.stderr)
            raw = ""

        if raw.strip() == "":
            return ""
        candidate = clean_company_name(raw)
        if is_valid_company_name(candidate) and not is_non_company_org(candidate):
            return candidate

        # First attempt produced something other than a bare, genuine
        # company name (malformed, or a school/university/government body);
        # retry once with a stricter prompt before giving up.
        system_prompt = COMPANY_EXTRACTION_STRICT_PROMPT

    return ""


def extract_company_names_llm(client: OpenAI, batch: list[InvestorRow]) -> None:
    """Populate `company_name` in-place for rows in batch that are missing it.

    Prefers deterministic, non-hallucinated reads over an LLM guess wherever
    that evidence is present — free, and strictly more reliable than an LLM
    (especially the low-quality default free-tier LLM backend this project
    falls back to when no OPENAI_BASE_URL is set):
      1. a structured "Experience:" field (quality.extract_company_from_snippet)
      2. an "<Title> at/of <Company>" phrase (quality.extract_company_from_at_pattern)
    Only falls through to the LLM when neither deterministic pass finds
    anything. Leaves company_name blank (never guesses) when nothing yields
    a confident answer.
    """
    for row in batch:
        if row.get("company_name"):
            continue
        text = row.get("summary", "") or ""
        deterministic = extract_company_from_snippet(text)
        if not deterministic:
            title_text = f"{row.get('profile_title', '') or ''} {text}"
            deterministic = extract_company_from_at_pattern(title_text)
        if deterministic and not is_non_company_org(deterministic):
            row["company_name"] = deterministic
            continue
        row["company_name"] = extract_company_name_llm(client, row)


# ---------------------------------------------------------------------------
# Age proxy extraction — LLM fallback (secondary to quality.extract_age_proxy,
# which is regex-based and runs first / is free). This is only ever a second
# pass over rows the regex pass couldn't resolve, and it is deliberately
# conservative: the model must quote the literal evidence text it used, and
# that quote is re-checked against the row's own text before being trusted —
# never a bare "the person is probably X" guess. If it can't point to real
# evidence, the row stays blank, exactly like the regex path.
# ---------------------------------------------------------------------------

AGE_EXTRACTION_PROMPT = (
    "You extract ONLY explicitly evidenced age information from profile "
    "text — you never guess or infer age from a name, a photo, a job title, "
    "or general impressions. Look for either (a) an explicitly stated age "
    "(e.g. 'Age: 34', '34 years old'), or (b) an explicitly stated "
    "graduation year (e.g. 'Class of 2014', 'graduated in 2014'). "
    'Return ONLY a JSON object: {"evidence_type":"explicit_age"|"graduation_year"|"none",'
    '"value":<integer or null>,"quote":"<the exact substring of the context that states it>"}. '
    "If neither is explicitly present, return "
    '{"evidence_type":"none","value":null,"quote":""}. '
    "Never fabricate a quote."
)


def extract_age_llm(client: OpenAI, row: InvestorRow) -> tuple[str, str, str]:
    """LLM fallback for age proxy extraction. Only used for rows the
    deterministic regex pass (quality.extract_age_proxy) couldn't resolve.

    Requires the model to quote its evidence verbatim; the quote is verified
    against the row's own text before being trusted at all. Returns
    ("", "", "none") whenever evidence can't be confirmed — this never
    invents an age.
    """
    text = f"{row.get('profile_title', '')} {row.get('summary', '')}".strip()
    if not text:
        return "", "", "none"

    prompt = f"{AGE_EXTRACTION_PROMPT}\n\nProfile text:\n{text[:2000]}\n\nJSON:"
    try:
        resp = client.chat.completions.create(
            model=_llm_model(),
            messages=[{"role": "user", "content": prompt}],
            max_tokens=150,
            temperature=0,
        )
        obj = extract_json_object(resp.choices[0].message.content or "")
    except Exception as exc:
        print(f"  llm age warn: {exc}", file=sys.stderr)
        return "", "", "none"

    if not obj:
        return "", "", "none"
    evidence_type = str(obj.get("evidence_type", "none"))
    quote = str(obj.get("quote", "") or "")
    value = obj.get("value")

    # Hard requirement: the quoted evidence must actually appear verbatim in
    # the row's own text. A model that can't produce a real quote gets no
    # credit — this is what keeps this path from ever inventing an age.
    if not quote or quote.lower() not in text.lower():
        return "", "", "none"
    if value is None:
        return "", "", "none"
    try:
        value = int(value)
    except (TypeError, ValueError):
        return "", "", "none"

    current_year = datetime.now(timezone.utc).year
    if evidence_type == "explicit_age" and 16 <= value <= 100:
        return str(value), f"explicit age stated in profile text (quoted: {quote!r})", "high"
    if evidence_type == "graduation_year" and 1950 <= value <= current_year:
        proxy_age = current_year - value + 22
        if 16 <= proxy_age <= 100:
            return (
                str(proxy_age),
                (
                    f"proxy estimated from stated graduation year {value} "
                    f"(quoted: {quote!r}; assumes ~22 years old at graduation) — not a stated age"
                ),
                "medium",
            )
    return "", "", "none"


def extract_ages_llm(client: OpenAI, batch: list[InvestorRow]) -> None:
    """Populate age/age_source/age_confidence in-place for rows in batch that
    have no usable proxy yet. Only ever a fallback after the free regex pass
    (quality.extract_age_proxy) has already run and found nothing.
    """
    for row in batch:
        if row.get("age") and str(row.get("age_confidence", "")).lower() not in ("", "none"):
            continue
        age, source, confidence = extract_age_llm(client, row)
        if age:
            row["age"], row["age_source"], row["age_confidence"] = age, source, confidence
