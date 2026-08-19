"""TargetConfig: the single configuration object that drives discovery.

This replaces the hard-coded CPA_TITLES / build_discovery_queries() constants
that used to live in config.py. Every campaign — CPA partners, SaaS founders,
angel investors, or anything else — is now just a different TargetConfig
instance, not a different code path.

Nothing in here is CPA-specific. The old CPA behaviour is preserved as a
*preset* (CPA_PARTNER_PRESET, at the bottom) built out of this same generic
schema, purely so the existing CLI keeps working unchanged if no config is
supplied.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


@dataclass
class TargetConfig:
    """User-facing campaign criteria. Maps directly to the UI config form.

    Every field is optional except `target_count`, so a caller can supply as
    little or as much as they know. Empty lists / None mean "no constraint on
    this dimension" — NOT "match nothing".
    """

    locations: list[str] = field(default_factory=lambda: ["United States"])
    titles: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)

    # --- optional synonym expansion -------------------------------------
    # Purely additive recall aids: each maps one of the *configured* base
    # terms above (titles/industries/keywords) to a list of alternative
    # phrasings that should be treated as equivalent evidence for that same
    # dimension -- e.g. title_synonyms={"Founder": ["Founder & CEO",
    # "Co-Founder", "Founding Partner"]}. A synonym key that doesn't match
    # any configured base term is ignored (never silently expands a
    # dimension the campaign didn't actually ask for). Used by both
    # query_generator.py (broader query phrasing) and quality.py's
    # matches_target_criteria (broader evidence matching) via the
    # expanded_titles/expanded_industries/expanded_keywords properties
    # below -- nothing here is hard-coded to any one vertical; a Fintech or
    # CPA campaign supplies its own synonym maps the same way.
    title_synonyms: dict[str, list[str]] = field(default_factory=dict)
    industry_synonyms: dict[str, list[str]] = field(default_factory=dict)
    keyword_synonyms: dict[str, list[str]] = field(default_factory=dict)

    company_size_min: int | None = None
    company_size_max: int | None = None

    # Age is a *proxy* filter applied downstream during qualification (Day 3+),
    # never invented — see quality.py age_source rules. Stored here only as
    # the user's requested bounds.
    age_min: int | None = None
    age_max: int | None = None

    target_count: int = 1000

    # What `target_count` counts. "raw" (default) is the original,
    # backward-compatible meaning: stop once `target_count` raw/discovered
    # candidates have been collected, regardless of how many go on to
    # qualify. "qualified" means the discovery loop should keep going
    # (subject to the budget fields below) until approximately
    # `target_count` candidates have actually QUALIFIED -- so
    # target_count=50 in "qualified" mode aims to produce ~50 usable
    # prospects, not 50 raw hits that mostly get filtered out.
    target_count_mode: str = "raw"

    # Budget controls for "qualified" mode (ignored in "raw" mode, where
    # target_count itself is already the hard stopping point). None means
    # "no explicit cap on this dimension" -- callers should still apply a
    # sane default rather than looping forever. These exist so
    # target_count_mode="qualified" with an unreachable target_count (e.g.
    # 5000 qualified prospects for a tiny niche) can't turn into unbounded
    # search-engine traffic.
    max_queries: int | None = None
    max_raw_candidates: int | None = None
    max_minutes: float | None = None

    # Free-form negative signal words, e.g. to exclude "recruiter", "student".
    exclude_keywords: list[str] = field(default_factory=list)

    # Optional human label, used for output file naming / campaign naming.
    name: str = "campaign"

    # --- optional Campaign metadata/templates -------------------------
    # None of these are required. They exist so a campaign's outreach
    # copy can be driven entirely by its TargetConfig JSON (the config
    # mechanism that already exists for discovery/qualification) instead
    # of hard-coding any vertical-specific subject/body text into generic
    # pipeline code. When omitted, campaign.ensure_campaign() falls back
    # to its own generic default templates. `campaign_name` defaults to
    # `name` above when unset.
    campaign_name: str | None = None
    campaign_description: str = ""
    email_subject_template: str | None = None
    email_body_template: str | None = None
    email_sender_name: str = ""

    def __post_init__(self) -> None:
        self.locations = _clean_list(self.locations)
        self.titles = _clean_list(self.titles)
        self.industries = _clean_list(self.industries)
        self.keywords = _clean_list(self.keywords)
        self.exclude_keywords = _clean_list(self.exclude_keywords)
        self.title_synonyms = _clean_synonym_map(self.title_synonyms)
        self.industry_synonyms = _clean_synonym_map(self.industry_synonyms)
        self.keyword_synonyms = _clean_synonym_map(self.keyword_synonyms)
        if self.target_count <= 0:
            raise ValueError("target_count must be a positive integer")
        if self.target_count_mode not in TARGET_COUNT_MODES:
            raise ValueError(
                f"target_count_mode must be one of {TARGET_COUNT_MODES!r}, "
                f"got {self.target_count_mode!r}"
            )
        if self.age_min is not None and self.age_max is not None:
            if self.age_min > self.age_max:
                raise ValueError("age_min cannot be greater than age_max")
        if self.company_size_min is not None and self.company_size_max is not None:
            if self.company_size_min > self.company_size_max:
                raise ValueError("company_size_min cannot be greater than company_size_max")

    # --- (de)serialization -------------------------------------------------

    # --- synonym-expanded views ---------------------------------------
    # What query_generator.py and quality.py's matches_target_criteria
    # actually match/search against -- the configured base terms plus
    # whatever synonyms were supplied for them. Equal to the base list
    # itself when no synonyms are configured, so nothing changes for an
    # existing config that doesn't use this feature.

    @property
    def expanded_titles(self) -> list[str]:
        return _expand_with_synonyms(self.titles, self.title_synonyms)

    @property
    def expanded_industries(self) -> list[str]:
        return _expand_with_synonyms(self.industries, self.industry_synonyms)

    @property
    def expanded_keywords(self) -> list[str]:
        return _expand_with_synonyms(self.keywords, self.keyword_synonyms)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TargetConfig":
        known = {f for f in cls.__dataclass_fields__}
        unknown = set(data) - known
        if unknown:
            raise ValueError(f"Unknown TargetConfig field(s): {', '.join(sorted(unknown))}")
        return cls(**data)

    @classmethod
    def from_json_file(cls, path: str | Path) -> "TargetConfig":
        p = Path(path)
        data = json.loads(p.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    def to_json_file(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    # --- output naming -------------------------------------------------

    def output_stem(self) -> str:
        """Filesystem-safe stem for this campaign's output CSV, e.g. 'saas_founders'."""
        import re

        slug = re.sub(r"[^a-z0-9]+", "_", self.name.lower()).strip("_")
        return slug or "campaign"


TARGET_COUNT_MODES = ("raw", "qualified")


def _clean_synonym_map(mapping: dict[str, list[str]] | None) -> dict[str, list[str]]:
    """Trim/drop-empty for a {base_term: [synonym, ...]} map, mirroring
    _clean_list()'s rules for the value lists."""
    if not mapping:
        return {}
    out: dict[str, list[str]] = {}
    for key, values in mapping.items():
        key = (key or "").strip()
        if not key:
            continue
        cleaned = _clean_list(values)
        if cleaned:
            out[key] = cleaned
    return out


def _expand_with_synonyms(base: list[str], synonyms: dict[str, list[str]]) -> list[str]:
    """base terms + every configured synonym for each, de-duplicated
    case-insensitively while preserving order. A synonym key that doesn't
    case-insensitively match one of `base`'s own terms is ignored, so a
    synonym map can't silently pull in evidence for a dimension the
    campaign didn't actually configure."""
    base_lower = {b.lower() for b in base}
    seen = set(base_lower)
    out = list(base)
    for key, values in synonyms.items():
        if key.strip().lower() not in base_lower:
            continue
        for v in values:
            low = v.lower()
            if low in seen:
                continue
            seen.add(low)
            out.append(v)
    return out


def _clean_list(values: list[str] | None) -> list[str]:
    """Trim, drop empties, de-dupe case-insensitively while preserving order."""
    if not values:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        v = (v or "").strip()
        if not v:
            continue
        key = v.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(v)
    return out


# ---------------------------------------------------------------------------
# Legacy preset: reproduces the exact criteria that used to be hard-coded in
# config.py (CPA_TITLES + the CPA-only query strings). Used only as the
# default when the orchestrator is run with no --config / no CLI overrides,
# so the existing CPA collection script keeps behaving identically.
# ---------------------------------------------------------------------------
CPA_PARTNER_PRESET = TargetConfig(
    name="us_cpa_partners",
    locations=["United States"],
    titles=[
        "managing partner", "name partner", "partner", "founding partner",
        "senior partner", "tax partner", "audit partner",
    ],
    industries=["CPA firm", "accounting firm", "public accounting"],
    keywords=[],
    target_count=5000,
)
