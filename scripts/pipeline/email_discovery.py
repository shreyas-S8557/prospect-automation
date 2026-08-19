"""Day 5: QUALIFIED -> EMAIL_CANDIDATES_FOUND / EMAIL_NOT_FOUND.

    QUALIFIED lead
        -> generate candidates (primary + supplementary generators)
        -> normalize + dedupe
        -> MX-validate domains (best-effort, supporting evidence)
        -> (optional) SMTP-check mailboxes (best-effort, supporting evidence)
        -> score + rank
        -> best usable candidate populates email/email_status/email_source/
           email_confidence -> EMAIL_CANDIDATES_FOUND
        -> no usable candidate -> EMAIL_NOT_FOUND

Engines, in the roles specified for Day 5:

  * ScrapegraphPatternGenerator (PRIMARY) — wraps the vendored pattern
    guesser already in this repo at Email_Finder/email_finder.py. This is
    real, wired-in candidate generation: it reuses that module's
    DEFAULT_PATTERNS, slugify_domain(), and looks_like_domain() directly so
    the pattern list and domain-guessing heuristics live in exactly one
    place (the vendored script), not duplicated here.

  * MailfoguessGenerator (SUPPLEMENTARY) — an integration seam for the
    Mailfoguess candidate generator. No Mailfoguess source was included in
    this repository/checkpoint, so this class does a soft `import
    mailfoguess`: if that package is present on the path (or injected via
    sys.modules, e.g. in a test) it's used automatically; if it is absent,
    `.available` is False and it contributes zero candidates. It never
    blocks the pipeline and is never a hard dependency.

  * EmailFinderMainGenerator (FALLBACK, isolated/optional) — the same soft
    `import email_finder_main` pattern as Mailfoguess, but only invoked by
    `generate_candidates_for_lead` when the primary + supplementary
    generators together produced zero usable candidates. Like Mailfoguess,
    no such module is vendored here; this is the integration point for it.

  * NodeMXChecker / NodeSMTPChecker — shell out to the vendored
    Email_Finder/verify_mx.js and verify_smtp.js (Node, zero deps) exactly
    as they exist in this repo, rather than reimplementing DNS/SMTP
    handling. Both degrade gracefully (UNKNOWN / NOT_CHECKED) if Node is
    unavailable, the scripts are missing, or the calls fail/time out — MX
    and SMTP are supporting evidence only (Day 5 spec items 5 and 6), never
    a hard gate on whether a candidate is usable, and never proof by
    themselves that an address is deliverable.

Nothing in scripts/pipeline/{models,lead_pipeline,lead_store,quality,
target_config,query_generator,orchestrator}.py's Day 2-4 behavior is
changed by this module. The one exception is LeadStore.save(), a new
additive method (see lead_store.py) needed because the existing
LeadStore.transition() only ever writes pipeline_status + updated_at — Day 5
also needs to persist email/email_source/email_confidence/company_domain,
which has no other public write path.

Day 6 (additive, see email_validation.py for the new stage itself):
  * Every candidate `generate_candidates_for_lead` produces — not just the
    winner — is now persisted via LeadStore.save_candidates(), keyed by
    lead_id and ordered by rank, carrying source(s), pattern(s), domain,
    MX/SMTP status, score, confidence, and a rolled-up `validation_status`
    (see classify_validation_status / VALIDATION_* constants below).
  * process_lead_email's own QUALIFIED -> EMAIL_CANDIDATES_FOUND /
    EMAIL_NOT_FOUND behavior is otherwise unchanged from Day 5 (still
    happy-path-selects a best candidate immediately, for backward
    compatibility with the Day 5 test suite and anything downstream already
    reading lead.email at that stage).
  * The *new* QUALIFIED-adjacent stage, EMAIL_CANDIDATES_FOUND ->
    EMAIL_VALIDATED, is added in email_validation.py. It never regenerates
    candidates — it only re-selects the best *persisted* one
    (select_best_row) and promotes the Lead. This is the only code path
    that ever writes PipelineStatus.EMAIL_VALIDATED.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

from .config import ROOT
from .lead_store import LeadStore
from .models import Lead, PipelineStatus
from .quality import (
    extract_company_from_at_pattern,
    extract_company_from_dash_pattern,
    extract_company_from_snippet,
    extract_domain_from_text,
    is_domain_guessable_company_name,
)

# ---------------------------------------------------------------------------
# Status vocabularies
# ---------------------------------------------------------------------------

# email_status values written onto Lead by this module. Free-text, NOT
# state-machine-validated (see models.Lead docstring) — distinct from
# pipeline_status.
EMAIL_STATUS_CANDIDATES_FOUND = "candidates_found"
EMAIL_STATUS_NOT_FOUND = "not_found"

# email_confidence labels, mirroring the existing age_confidence convention
# in quality.py (AGE_CONFIDENCE_LEVELS = ("high", "medium", "low", "none")).
EMAIL_CONFIDENCE_LEVELS = ("high", "medium", "low", "none")

MX_VALID = "VALID"
MX_DEAD = "DEAD"
MX_UNKNOWN = "UNKNOWN"  # not checked / checker unavailable / lookup failed

SMTP_EXISTS = "EXISTS"
SMTP_NOT_EXISTS = "NOT_EXISTS"
SMTP_CATCH_ALL = "CATCH_ALL"
SMTP_UNKNOWN = "UNKNOWN"  # checked but inconclusive (timeout, greylisting, etc.)
SMTP_NOT_CHECKED = "NOT_CHECKED"  # never attempted (disabled, or MX already DEAD)

# ---------------------------------------------------------------------------
# Day 6: candidate validation_status — a single, audit-friendly label per
# candidate distinguishing *how far* it has been checked from *what the
# check found*. This is deliberately a separate concept from mx_status /
# smtp_status: those are raw per-signal evidence (item 5/6 of the Day 6
# spec), while validation_status is the human-facing rollup used for
# persistence, resumability, and best-candidate selection.
#
#   GENERATED          candidate produced by a generator; no MX or SMTP
#                       check has been attempted against it yet.
#   DOMAIN_VALID        MX lookup succeeded (mail is deliverable to the
#                       domain) but the mailbox itself hasn't been confirmed
#                       by SMTP (either SMTP is disabled, or it ran and was
#                       inconclusive is handled separately below).
#   SMTP_CONFIRMED      SMTP RCPT-TO reported the mailbox exists. Strongest
#                       available signal, but still not proof of ongoing
#                       deliverability (item 6) — a mailbox can exist today
#                       and bounce tomorrow.
#   SMTP_INCONCLUSIVE   SMTP was attempted but returned an ambiguous result
#                       (timeout, greylisting, catch-all domain, etc.).
#   INVALID             MX lookup proved the domain dead, or SMTP proved the
#                       mailbox doesn't exist. The only two disqualifying
#                       outcomes (mirrors is_usable()) — an INVALID
#                       candidate can never become the selected email
#                       (item 7).
#   UNKNOWN             A check was attempted (MX and/or SMTP) but came back
#                       inconclusive/unavailable rather than never having
#                       been attempted at all — distinct from GENERATED so
#                       "we tried and don't know" is never confused with
#                       "we haven't looked yet".
# ---------------------------------------------------------------------------

VALIDATION_GENERATED = "GENERATED"
VALIDATION_DOMAIN_VALID = "DOMAIN_VALID"
VALIDATION_SMTP_CONFIRMED = "SMTP_CONFIRMED"
VALIDATION_SMTP_INCONCLUSIVE = "SMTP_INCONCLUSIVE"
VALIDATION_INVALID = "INVALID"
VALIDATION_UNKNOWN = "UNKNOWN"

VALIDATION_STATUSES = (
    VALIDATION_GENERATED,
    VALIDATION_DOMAIN_VALID,
    VALIDATION_SMTP_CONFIRMED,
    VALIDATION_SMTP_INCONCLUSIVE,
    VALIDATION_INVALID,
    VALIDATION_UNKNOWN,
)


# ---------------------------------------------------------------------------
# Vendor bridge: load Email_Finder/email_finder.py (the primary, already-
# present-in-this-repo pattern guesser) without turning it into an
# importable package — it lives outside scripts/pipeline and has no
# __init__.py, so a normal `from Email_Finder import email_finder` import
# isn't available; we load it by file path instead.
# ---------------------------------------------------------------------------

VENDOR_EMAIL_FINDER_PATH = ROOT / "Email_Finder" / "email_finder.py"


def _load_vendor_email_finder():
    spec = importlib.util.spec_from_file_location(
        "_vendor_scrapegraph_email_finder", VENDOR_EMAIL_FINDER_PATH
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load vendored email finder at {VENDOR_EMAIL_FINDER_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    _vendor = _load_vendor_email_finder()
    VENDOR_EMAIL_FINDER_AVAILABLE = True
except Exception:  # missing file, syntax error in vendor script, etc.
    _vendor = None
    VENDOR_EMAIL_FINDER_AVAILABLE = False


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# Same placeholder-domain rejection list as quality.extract_email(), plus a
# couple of common guess-artifact placeholders, kept local to this module
# since it's specific to *generated* candidates rather than free-text
# scraping.
_PLACEHOLDER_DOMAIN_MARKERS = (
    "example.com", "email.com", "domain.com", "sentry",
    "test.com", "yourcompany.com", "company.com",
)


def normalize_email(raw: str) -> str:
    """Lowercase/trim a raw candidate email; return "" if it isn't usable.

    Returns "" (never raises) for: empty input, malformed addresses, and
    obvious placeholder domains — callers filter on truthiness.
    """
    text = (raw or "").strip().strip("<>").lower()
    if not text or not _EMAIL_RE.match(text):
        return ""
    domain = text.rsplit("@", 1)[-1]
    if any(marker in domain for marker in _PLACEHOLDER_DOMAIN_MARKERS):
        return ""
    return text


# ---------------------------------------------------------------------------
# Candidate model
# ---------------------------------------------------------------------------


@dataclass
class EmailCandidate:
    """One candidate email address for a Lead, with provenance and evidence."""

    email: str
    sources: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    domain: str = ""
    domain_guessed: bool = False
    mx_status: str = MX_UNKNOWN
    smtp_status: str = SMTP_NOT_CHECKED
    score: float = 0.0

    # Day 6: whether an MX / SMTP check was actually *attempted* for this
    # candidate (regardless of what it found). This is what lets
    # classify_validation_status() distinguish GENERATED ("never checked")
    # from UNKNOWN ("checked, but inconclusive") — mx_status/smtp_status
    # alone can't make that distinction, since MX_UNKNOWN and SMTP_UNKNOWN
    # are also the graceful-degradation defaults used before any check runs.
    mx_checked: bool = False
    smtp_checked: bool = False

    @property
    def source(self) -> str:
        """Combined source label, e.g. "scrapegraph_pattern+mailfoguess" —
        this is what's written to Lead.email_source."""
        return "+".join(self.sources)

    @property
    def validation_status(self) -> str:
        return classify_validation_status(self)


def dedupe_candidates(candidates: Iterable[EmailCandidate]) -> list[EmailCandidate]:
    """Merge candidates that resolved to the same normalized email address.

    Order-preserving (first-seen order). Sources and patterns are unioned
    (preserving first-seen order within each); domain/domain_guessed are
    filled in from whichever contributing candidate had them first. A
    candidate agreed on by more than one generator is a stronger signal —
    see score_candidate(), which rewards multi-source agreement.
    """
    merged: dict[str, EmailCandidate] = {}
    order: list[str] = []
    for c in candidates:
        if not c.email:
            continue
        if c.email not in merged:
            merged[c.email] = EmailCandidate(
                email=c.email,
                sources=tuple(dict.fromkeys(c.sources)),
                patterns=tuple(dict.fromkeys(c.patterns)),
                domain=c.domain,
                domain_guessed=c.domain_guessed,
                score=c.score,
            )
            order.append(c.email)
        else:
            existing = merged[c.email]
            existing.sources = tuple(dict.fromkeys(existing.sources + c.sources))
            existing.patterns = tuple(dict.fromkeys(existing.patterns + c.patterns))
            if not existing.domain and c.domain:
                existing.domain = c.domain
                existing.domain_guessed = c.domain_guessed
    return [merged[e] for e in order]


# ---------------------------------------------------------------------------
# Candidate generators
# ---------------------------------------------------------------------------


class CandidateGenerator(Protocol):
    name: str

    def generate(self, lead: Lead) -> list[EmailCandidate]: ...


def _clean_name_part(value: str) -> str:
    return re.sub(r"[^a-z\-]", "", (value or "").strip().lower())


class ScrapegraphPatternGenerator:
    """PRIMARY generator: the vendored Email_Finder/email_finder.py pattern
    guesser, adapted to read from a Lead instead of a CSV row.

    Mirrors that module's build_candidates()/DEFAULT_PATTERNS algorithm
    exactly (same patterns, same local-part construction), but tracks which
    pattern produced each candidate — build_candidates() itself only
    returns a flat, already-deduped list of strings, with no per-candidate
    attribution, which score_candidate() needs (earlier/more common
    patterns like "{first}.{last}" score higher than rarer ones).
    """

    name = "scrapegraph_pattern"

    def __init__(self, patterns: list[str] | None = None):
        if not VENDOR_EMAIL_FINDER_AVAILABLE:
            raise RuntimeError(
                f"Vendored email finder not available at {VENDOR_EMAIL_FINDER_PATH}"
            )
        self._patterns = list(patterns) if patterns is not None else list(_vendor.DEFAULT_PATTERNS)

    # This vendored pattern generator's slugify_domain() always appends a
    # hard-coded ".com" — a bad prior for this project's actual ICP (AI/SaaS
    # startups very commonly sit on .ai/.io/.co, e.g. a real "Loopwave AI"
    # is far more likely to be loopwave.ai than loopwaveai.com). Guessing
    # only .com means the *correct* domain is never even generated for a
    # large share of leads, so no amount of downstream MX checking can save
    # it — MX checking can only rule candidates in/out among the ones that
    # were actually tried. Trying a small set of the most common startup
    # TLDs and letting the existing MX check (already wired into
    # generate_candidates_for_lead(), not something new here) disqualify
    # the wrong ones is a much better bet than committing to one TLD guess.
    _GUESS_TLDS = (".com", ".ai", ".io", ".co")

    def _resolve_domains(self, lead: Lead) -> tuple[list[str], bool]:
        """Returns (domains, was_guessed). Priority, strongest evidence
        first:
          1. An explicit, already-domain-shaped company_domain/company_name
             -- trusted outright, exactly as before.
          2. A real domain literally mentioned in the lead's own evidence
             text (job_title/profile_summary) -- e.g. "I'm building
             SaasRise (www.saasrise.com)". This isn't a guess at all, so
             it's tried even when company_name itself is missing/corrupted.
          3. A slugified guess from company_name -- but only after
             recovering a clean company name if company_name looks
             corrupted (see below), and only if the resulting name passes
             is_domain_guessable_company_name(): a domain is never guessed
             from an empty, non-company, or bare-generic-term company name
             (e.g. never "AI" -> ai.com, never a university) -- see that
             function's docstring for why guessing anyway is worse than
             not guessing.

        Corrupted company_name recovery: upstream extraction sometimes
        glues the lead's own name/location onto the real company name
        (e.g. company_name="Hebbia George Sivulka" for a lead actually
        named George Sivulka, or "SaasRise Ryan Allis. Austin"). When that
        happens, job_title commonly still carries the clean "<Title> at
        <Company>" phrasing (e.g. "Founder & CEO at Hebbia") even though
        company_name doesn't -- re-deriving from there with the same
        deterministic, already-tested extractors quality.py's LLM-fallback
        path already uses (extract_company_from_at_pattern /
        extract_company_from_snippet) recovers the real company rather
        than guessing off the corrupted field or giving up entirely.
        """
        if lead.company_domain and _vendor.looks_like_domain(lead.company_domain):
            return [lead.company_domain.lower().strip()], False
        if lead.company_name and _vendor.looks_like_domain(lead.company_name):
            return [lead.company_name.lower().strip()], False

        evidence_domain = extract_domain_from_text(f"{lead.job_title} {lead.profile_summary}")
        if evidence_domain:
            return [evidence_domain], False

        company_for_guess = lead.company_name
        recovered = (
            extract_company_from_at_pattern(lead.job_title)
            or extract_company_from_dash_pattern(lead.job_title)
            or extract_company_from_snippet(lead.profile_summary)
        )
        if recovered and recovered.lower() != (company_for_guess or "").lower():
            # Prefer the recovered name when: there's no company_name at
            # all yet, the raw company_name reads as *not* guessable
            # (generic term, university, malformed), or the recovered name
            # is literally a substring of company_name -- strong evidence
            # company_name is exactly "<recovered><glued-on extra text>"
            # (e.g. "Hebbia George Sivulka" contains "Hebbia"; "SaasRise
            # Ryan Allis. Austin" contains "SaasRise"), i.e. corrupted
            # rather than a genuinely different/better name.
            raw_compact = re.sub(r"[^a-z0-9]", "", company_for_guess.lower()) if company_for_guess else ""
            recovered_compact = re.sub(r"[^a-z0-9]", "", recovered.lower())
            if (
                not company_for_guess
                or not is_domain_guessable_company_name(company_for_guess)
                or (recovered_compact and recovered_compact in raw_compact)
            ):
                company_for_guess = recovered

        if not is_domain_guessable_company_name(company_for_guess):
            return [], False

        guessed_com = _vendor.slugify_domain(company_for_guess)
        slug = guessed_com[: -len(".com")] if guessed_com.endswith(".com") else guessed_com
        if not slug:
            return [], False
        domains = [f"{slug}{tld}" for tld in self._GUESS_TLDS]
        return domains, True

    def generate(self, lead: Lead) -> list[EmailCandidate]:
        first = _clean_name_part(lead.first_name)
        last = _clean_name_part(lead.last_name)
        if not first and lead.full_name:
            guessed_first, guessed_last = _vendor.split_name(lead.full_name)
            first = first or _clean_name_part(guessed_first)
            last = last or _clean_name_part(guessed_last)

        domains, guessed = self._resolve_domains(lead)
        if not first or not domains:
            return []

        f, l = first[:1], last[:1]
        n = len(self._patterns)
        seen: set[str] = set()
        out: list[EmailCandidate] = []
        for domain_idx, domain in enumerate(domains):
            for idx, pat in enumerate(self._patterns):
                try:
                    local = pat.format(first=first, last=last, f=f, l=l)
                except (KeyError, IndexError):
                    continue
                if not local or (not last and "{last}" in pat):
                    continue
                norm = normalize_email(f"{local}@{domain}")
                if not norm or norm in seen:
                    continue
                seen.add(norm)
                # Earlier patterns are the more common professional
                # convention (see DEFAULT_PATTERNS ordering) -> higher base
                # score. When multiple TLDs were guessed, also apply a small
                # ordering prior favoring the more common TLD (.com over
                # .ai/.io/.co) — this only matters as a tie-breaker when MX
                # checking can't disambiguate (e.g. Node isn't installed);
                # score_candidate()'s +0.2/-1.0 MX signal otherwise dominates
                # and correctly overrides this prior once a real domain is
                # confirmed.
                base = 1.0 - (idx / max(n, 1)) * 0.5
                if guessed:
                    base -= 0.15  # slugified-guess domains are unreliable (vendor's own caveat)
                    base -= 0.03 * domain_idx  # small same-TLD-tried-first prior
                out.append(
                    EmailCandidate(
                        email=norm,
                        sources=(self.name,),
                        patterns=(pat,),
                        domain=domain,
                        domain_guessed=guessed,
                        score=max(0.05, base),
                    )
                )
        return out


class _SoftImportGenerator:
    """Shared base for optional, non-vendored candidate generators
    (Mailfoguess, email-finder-main). Neither ships in this repository —
    this base class implements the "isolated, not a hard dependency"
    contract: a missing module means `.available` is False and `.generate()`
    returns [] rather than raising, so the pipeline runs unaffected whether
    or not the real package is ever installed.
    """

    module_name = ""
    name = ""
    base_score = 0.4

    def __init__(self):
        self._backend = None
        try:
            self._backend = __import__(self.module_name)
        except ImportError:
            self._backend = None

    @property
    def available(self) -> bool:
        return self._backend is not None

    def _call_backend(self, lead: Lead):
        """Subclasses implement the actual backend call; wrapped centrally
        so a misbehaving optional backend (wrong signature, raises, returns
        garbage) degrades to "no candidates" instead of crashing the
        pipeline."""
        raise NotImplementedError

    def generate(self, lead: Lead) -> list[EmailCandidate]:
        if not self.available:
            return []
        try:
            raw = self._call_backend(lead)
        except Exception:
            return []
        out: list[EmailCandidate] = []
        for item in raw or []:
            email = item.get("email") if isinstance(item, dict) else str(item)
            norm = normalize_email(email)
            if not norm:
                continue
            out.append(
                EmailCandidate(
                    email=norm,
                    sources=(self.name,),
                    domain=norm.rsplit("@", 1)[-1],
                    score=self.base_score,
                )
            )
        return out


class MailfoguessGenerator(_SoftImportGenerator):
    """SUPPLEMENTARY generator — integration seam for Mailfoguess.

    No Mailfoguess source is vendored in this repository/checkpoint. If a
    `mailfoguess` package is importable (installed for real, or injected
    into sys.modules for a test), it's expected to expose a
    `find_candidates(first_name=..., last_name=..., company_domain=...)`
    call returning an iterable of emails or {"email": ...} dicts.
    """

    module_name = "mailfoguess"
    name = "mailfoguess"
    base_score = 0.5

    def _call_backend(self, lead: Lead):
        return self._backend.find_candidates(
            first_name=lead.first_name,
            last_name=lead.last_name,
            company_domain=lead.company_domain or lead.company_name,
        )


class EmailFinderMainGenerator(_SoftImportGenerator):
    """FALLBACK generator (isolated, optional) — integration seam for
    `email-finder-main`.

    No email-finder-main source is vendored in this repository/checkpoint.
    Even when available, this generator is only invoked by
    generate_candidates_for_lead() as a fallback, after the primary +
    supplementary generators together produce zero usable candidates — it
    is never a hard dependency of the happy path. If a real
    `email_finder_main` package is importable, it's expected to expose a
    `find(first_name=..., last_name=..., company_name=..., company_domain=...)`
    call returning an iterable of emails or {"email": ...} dicts.
    """

    module_name = "email_finder_main"
    name = "email_finder_main"
    base_score = 0.3

    def _call_backend(self, lead: Lead):
        return self._backend.find(
            first_name=lead.first_name,
            last_name=lead.last_name,
            company_name=lead.company_name,
            company_domain=lead.company_domain,
        )


def default_primary_generators() -> list[CandidateGenerator]:
    return [ScrapegraphPatternGenerator()] if VENDOR_EMAIL_FINDER_AVAILABLE else []


def default_supplementary_generators() -> list[CandidateGenerator]:
    return [MailfoguessGenerator()]


def default_fallback_generator() -> CandidateGenerator:
    return EmailFinderMainGenerator()


# ---------------------------------------------------------------------------
# Domain/MX validation (best-effort, supporting evidence — item 5)
# ---------------------------------------------------------------------------


class MXChecker(Protocol):
    def check_domains(self, domains: list[str]) -> dict[str, str]: ...


class NullMXChecker:
    """No-op checker: every domain comes back UNKNOWN. Used when MX
    validation isn't supported/wanted rather than skipping the step
    silently in a way that looks like a real DEAD verdict."""

    def check_domains(self, domains: list[str]) -> dict[str, str]:
        return {d: MX_UNKNOWN for d in domains}


class NodeMXChecker:
    """Wraps the vendored Email_Finder/verify_mx.js exactly as it exists in
    this repo (Node, zero deps) rather than reimplementing DNS MX lookups.

    Best-effort: if Node isn't on PATH, the script is missing, or the call
    fails/times out, every domain comes back MX_UNKNOWN — this checker
    never raises and never blocks the pipeline (Day 5 item 5: "where
    already supported").
    """

    def __init__(self, script_path: Path | None = None, timeout: float = 20.0):
        self._script = script_path or (ROOT / "Email_Finder" / "verify_mx.js")
        self._timeout = timeout
        self._node = shutil.which("node")

    @property
    def available(self) -> bool:
        return bool(self._node) and self._script.exists()

    def check_domains(self, domains: list[str]) -> dict[str, str]:
        domains = sorted(set(d for d in domains if d))
        if not domains:
            return {}
        if not self.available:
            return {d: MX_UNKNOWN for d in domains}

        probe_emails = [f"probe@{d}" for d in domains]
        try:
            with tempfile.TemporaryDirectory() as tmp:
                in_path = Path(tmp) / "emails.json"
                out_path = Path(tmp) / "mx_clean.json"
                in_path.write_text(json.dumps(probe_emails), encoding="utf-8")
                subprocess.run(
                    [self._node, str(self._script), str(in_path), "-o", str(out_path)],
                    cwd=str(self._script.parent),
                    capture_output=True,
                    timeout=self._timeout,
                    check=False,
                )
                if not out_path.exists():
                    return {d: MX_UNKNOWN for d in domains}
                parsed = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:
            return {d: MX_UNKNOWN for d in domains}

        # verify_mx.js writes {"valid": [...], "dead": [...], "unknown": [...]}
        # — "unknown" covers domains whose DNS lookup itself failed for an
        # infrastructure reason (timeout, no network, DNS server down,
        # rate limiting) rather than a confirmed "no such domain/record"
        # response, and must never be read as DEAD (that would silently
        # disqualify every real candidate the moment DNS is flaky/blocked
        # — see the wider MXChecker docstring). Falls back to treating a
        # bare list (the old output format) as an all-or-nothing valid set,
        # for compatibility with any external tooling still on the old
        # verify_mx.js.
        if isinstance(parsed, dict):
            valid_emails = parsed.get("valid", [])
            unknown_emails = parsed.get("unknown", [])
        else:
            valid_emails = parsed
            unknown_emails = []

        valid_domains = {e.rsplit("@", 1)[-1] for e in valid_emails if isinstance(e, str) and "@" in e}
        unknown_domains = {e.rsplit("@", 1)[-1] for e in unknown_emails if isinstance(e, str) and "@" in e}

        def _status(d: str) -> str:
            if d in valid_domains:
                return MX_VALID
            if d in unknown_domains:
                return MX_UNKNOWN
            return MX_DEAD

        return {d: _status(d) for d in domains}


# ---------------------------------------------------------------------------
# SMTP checking (best-effort, supporting evidence ONLY — item 6). Disabled
# by default: real SMTP RCPT-TO checks are slow (one handshake per mailbox)
# and outbound port 25 is blocked on most cloud/sandboxed hosts, which is
# exactly why the spec restricts SMTP to supporting evidence rather than a
# required gate. Callers opt in with enable_smtp=True.
# ---------------------------------------------------------------------------


class SMTPChecker(Protocol):
    def check_emails(self, emails: list[str]) -> dict[str, str]: ...


class NullSMTPChecker:
    def check_emails(self, emails: list[str]) -> dict[str, str]:
        return {e: SMTP_NOT_CHECKED for e in emails}


_SMTP_PER_EMAIL_RE = re.compile(
    r"(?P<email>[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+)\s+\W\s+(?P<verdict>EXISTS|NOT_EXISTS|TIMEOUT|CONN_ERROR|"
    r"REJECTED|EHLO_FAIL|MAIL_FROM_FAIL|TEMP_FAIL|RATE_LIMITED|UNKNOWN)\b"
)
_SMTP_CATCH_ALL_DOMAIN_RE = re.compile(r"\W\s+(?P<domain>[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\s+\W\s+catch-all")


class NodeSMTPChecker:
    """Wraps the vendored Email_Finder/verify_smtp.js exactly as it exists
    in this repo. Parses its per-line console output (there's no
    per-address JSON output — verify_smtp.js only writes the merged
    "safe to send" list) to recover EXISTS/NOT_EXISTS/CATCH_ALL/UNKNOWN per
    address. Never raises: anything unparsed comes back SMTP_UNKNOWN, and
    connection failures (no network, port 25 blocked, etc.) surface as
    SMTP_UNKNOWN too — exactly the "supporting evidence only" contract in
    item 6, since an inconclusive SMTP result must never read as proof of
    anything.
    """

    def __init__(self, script_path: Path | None = None, timeout: float = 30.0, smtp_timeout_ms: int = 5000):
        self._script = script_path or (ROOT / "Email_Finder" / "verify_smtp.js")
        self._timeout = timeout
        self._smtp_timeout_ms = smtp_timeout_ms
        self._node = shutil.which("node")

    @property
    def available(self) -> bool:
        return bool(self._node) and self._script.exists()

    def check_emails(self, emails: list[str]) -> dict[str, str]:
        emails = [e for e in emails if e]
        if not emails:
            return {}
        if not self.available:
            return {e: SMTP_UNKNOWN for e in emails}

        env = dict(os.environ, SMTP_TIMEOUT=str(self._smtp_timeout_ms))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                in_path = Path(tmp) / "emails.json"
                out_path = Path(tmp) / "emails_verified.json"
                in_path.write_text(json.dumps(emails), encoding="utf-8")
                proc = subprocess.run(
                    [self._node, str(self._script), str(in_path), "-o", str(out_path)],
                    cwd=str(self._script.parent),
                    capture_output=True,
                    timeout=self._timeout,
                    check=False,
                    text=True,
                    env=env,
                )
        except Exception:
            return {e: SMTP_UNKNOWN for e in emails}

        stdout = proc.stdout or ""
        result = {e: SMTP_UNKNOWN for e in emails}

        # Domain-level catch-all lines apply to every email at that domain.
        domain_to_emails: dict[str, list[str]] = {}
        for e in emails:
            domain_to_emails.setdefault(e.rsplit("@", 1)[-1], []).append(e)
        for m in _SMTP_CATCH_ALL_DOMAIN_RE.finditer(stdout):
            for e in domain_to_emails.get(m.group("domain"), []):
                result[e] = SMTP_CATCH_ALL

        # Per-email verdict lines override the domain-level default.
        for m in _SMTP_PER_EMAIL_RE.finditer(stdout):
            email = m.group("email").lower()
            verdict = m.group("verdict")
            if email not in result:
                continue
            if verdict == "EXISTS":
                result[email] = SMTP_EXISTS
            elif verdict == "NOT_EXISTS":
                result[email] = SMTP_NOT_EXISTS
            else:
                result[email] = SMTP_UNKNOWN
        return result


# ---------------------------------------------------------------------------
# Scoring / ranking (item 4)
# ---------------------------------------------------------------------------


def score_candidate(candidate: EmailCandidate) -> float:
    """Combine generator confidence + cross-source agreement + MX/SMTP
    evidence into a single 0.0-1.0 score. MX_DEAD and SMTP_NOT_EXISTS are
    the only two signals treated as disqualifying (see is_usable) — every
    other signal only nudges the score, never proves or disproves deliverability.
    """
    score = candidate.score if candidate.score else 0.4

    if len(candidate.sources) > 1:
        score += 0.15 * (len(candidate.sources) - 1)

    if candidate.mx_status == MX_VALID:
        score += 0.2
    elif candidate.mx_status == MX_DEAD:
        score -= 1.0  # disqualifying, see is_usable()

    if candidate.smtp_status == SMTP_EXISTS:
        score += 0.15
    elif candidate.smtp_status == SMTP_CATCH_ALL:
        score += 0.03  # weak positive: mailbox unconfirmed, domain accepts anything
    elif candidate.smtp_status == SMTP_NOT_EXISTS:
        score -= 1.0  # disqualifying, see is_usable()

    return max(0.0, min(1.0, score))


def is_usable(candidate: EmailCandidate) -> bool:
    """A candidate is usable unless something has actively disproven it.
    MX_UNKNOWN and SMTP_UNKNOWN/SMTP_NOT_CHECKED are "we don't know" —
    never treated as proof of anything, so they don't disqualify."""
    if candidate.mx_status == MX_DEAD:
        return False
    if candidate.smtp_status == SMTP_NOT_EXISTS:
        return False
    return True


def classify_validation_status(candidate: EmailCandidate) -> str:
    """Roll a candidate's raw mx_status/smtp_status/*_checked evidence up
    into one of the six audit states (item 5). Order matters: disqualifying
    evidence (INVALID) always wins, even if some other signal looks
    favorable, so this stays consistent with is_usable()/is_usable()-driven
    selection (item 7) — a candidate is VALIDATION_INVALID if and only if
    is_usable() is False.
    """
    if candidate.mx_status == MX_DEAD or candidate.smtp_status == SMTP_NOT_EXISTS:
        return VALIDATION_INVALID
    if candidate.smtp_status == SMTP_EXISTS:
        return VALIDATION_SMTP_CONFIRMED
    if candidate.smtp_checked and candidate.smtp_status in (SMTP_UNKNOWN, SMTP_CATCH_ALL):
        return VALIDATION_SMTP_INCONCLUSIVE
    if candidate.mx_status == MX_VALID:
        return VALIDATION_DOMAIN_VALID
    if not candidate.mx_checked and not candidate.smtp_checked:
        return VALIDATION_GENERATED
    return VALIDATION_UNKNOWN


def confidence_label(candidate: EmailCandidate) -> str:
    if not is_usable(candidate):
        return "none"
    if candidate.score >= 0.75:
        return "high"
    if candidate.score >= 0.5:
        return "medium"
    if candidate.score > 0:
        return "low"
    return "none"


def rank_candidates(candidates: Iterable[EmailCandidate]) -> list[EmailCandidate]:
    """Score every candidate and return them sorted best-first (stable for
    ties, so generation order breaks ties deterministically)."""
    scored = list(candidates)
    for c in scored:
        c.score = score_candidate(c)
    return sorted(scored, key=lambda c: c.score, reverse=True)


# ---------------------------------------------------------------------------
# Orchestration: one Lead -> ranked candidates (+ best usable one)
# ---------------------------------------------------------------------------


@dataclass
class EmailDiscoveryResult:
    lead_id: str
    candidates: list[EmailCandidate] = field(default_factory=list)
    best: EmailCandidate | None = None
    used_fallback: bool = False
    resolved_domain: str = ""


# ---------------------------------------------------------------------------
# Day 6: candidate persistence — row <-> EmailCandidate conversion.
#
# EmailCandidate (above) stays the in-memory, generation-time shape (Day 5,
# unchanged). CandidateRow is the *persisted* shape: an EmailCandidate plus
# the bookkeeping LeadStore needs (which lead it belongs to, its rank within
# that lead's ranked list, whether it was the selected "best", and when it
# was written). LeadStore itself only ever sees plain dicts (candidate_to_row
# / candidate_from_row) — it has no import-time dependency on this module,
# which keeps lead_store.py generic and avoids a circular import (this
# module already imports LeadStore).
# ---------------------------------------------------------------------------

CANDIDATE_ROW_FIELDS = [
    "candidate_id",
    "lead_id",
    "rank",
    "email",
    "sources",
    "patterns",
    "domain",
    "domain_guessed",
    "mx_status",
    "smtp_status",
    "mx_checked",
    "smtp_checked",
    "score",
    "confidence",
    "validation_status",
    "is_best",
    "created_at",
]


@dataclass
class CandidateRow:
    """A persisted EmailCandidate, as read back from LeadStore. Carries
    everything EmailCandidate does, plus the persistence-only bookkeeping
    fields (rank/is_best/candidate_id/created_at)."""

    candidate_id: str
    lead_id: str
    rank: int
    email: str
    sources: tuple[str, ...]
    patterns: tuple[str, ...]
    domain: str
    domain_guessed: bool
    mx_status: str
    smtp_status: str
    mx_checked: bool
    smtp_checked: bool
    score: float
    confidence: str
    validation_status: str
    is_best: bool
    created_at: str

    def to_candidate(self) -> EmailCandidate:
        """Reconstruct the plain EmailCandidate (drops the persistence-only
        bookkeeping fields) — e.g. for re-scoring or display."""
        return EmailCandidate(
            email=self.email,
            sources=self.sources,
            patterns=self.patterns,
            domain=self.domain,
            domain_guessed=self.domain_guessed,
            mx_status=self.mx_status,
            smtp_status=self.smtp_status,
            mx_checked=self.mx_checked,
            smtp_checked=self.smtp_checked,
            score=self.score,
        )


def candidate_to_row(
    candidate: EmailCandidate,
    *,
    lead_id: str,
    rank: int,
    is_best: bool,
    candidate_id: str | None = None,
    created_at: str | None = None,
) -> dict:
    """EmailCandidate -> plain dict ready for LeadStore.save_candidates().
    Tuples are JSON-encoded (sources/patterns) and booleans become 0/1 so
    the row is SQLite-native; LeadStore never needs to know EmailCandidate
    exists."""
    from .models import utc_now_iso

    return {
        "candidate_id": candidate_id or f"{lead_id}:{uuid_hex()}",
        "lead_id": lead_id,
        "rank": rank,
        "email": candidate.email,
        "sources": json.dumps(list(candidate.sources)),
        "patterns": json.dumps(list(candidate.patterns)),
        "domain": candidate.domain,
        "domain_guessed": int(bool(candidate.domain_guessed)),
        "mx_status": candidate.mx_status,
        "smtp_status": candidate.smtp_status,
        "mx_checked": int(bool(candidate.mx_checked)),
        "smtp_checked": int(bool(candidate.smtp_checked)),
        "score": float(candidate.score),
        "confidence": confidence_label(candidate),
        "validation_status": classify_validation_status(candidate),
        "is_best": int(bool(is_best)),
        "created_at": created_at or utc_now_iso(),
    }


def candidate_row_from_dict(row: dict) -> CandidateRow:
    """Plain dict (as read back from LeadStore) -> CandidateRow."""
    return CandidateRow(
        candidate_id=row["candidate_id"],
        lead_id=row["lead_id"],
        rank=int(row["rank"]),
        email=row["email"],
        sources=tuple(json.loads(row["sources"]) if row["sources"] else ()),
        patterns=tuple(json.loads(row["patterns"]) if row["patterns"] else ()),
        domain=row["domain"],
        domain_guessed=bool(int(row["domain_guessed"])),
        mx_status=row["mx_status"],
        smtp_status=row["smtp_status"],
        mx_checked=bool(int(row["mx_checked"])),
        smtp_checked=bool(int(row["smtp_checked"])),
        score=float(row["score"]),
        confidence=row["confidence"],
        validation_status=row["validation_status"],
        is_best=bool(int(row["is_best"])),
        created_at=row["created_at"],
    )


def uuid_hex() -> str:
    import uuid as _uuid

    return _uuid.uuid4().hex


def candidates_to_rows(lead_id: str, ranked_candidates: list[EmailCandidate]) -> list[dict]:
    """Ranked (best-first) candidate list -> rows ready for
    LeadStore.save_candidates(). `rank` is the 0-based position in the
    ranked list (item 4: this is what lets the best usable candidate be
    re-selected later without re-running generation/scoring); `is_best`
    marks the single highest-ranked *usable* candidate, if any (item 7:
    an INVALID candidate is never marked is_best even if nothing else
    exists)."""
    usable_ranks = [i for i, c in enumerate(ranked_candidates) if is_usable(c)]
    best_rank = usable_ranks[0] if usable_ranks else None
    return [
        candidate_to_row(c, lead_id=lead_id, rank=i, is_best=(i == best_rank))
        for i, c in enumerate(ranked_candidates)
    ]


def select_best_row(rows: list[CandidateRow]) -> CandidateRow | None:
    """Pick the best usable candidate from already-persisted rows, without
    regenerating or re-scoring anything (item 4). Rows are expected in
    ascending `rank` order (LeadStore.list_candidates guarantees this); the
    first row already flagged is_best is authoritative, but this also
    tolerates being handed unsorted/legacy rows by falling back to a fresh
    scan for the lowest-rank row whose validation_status isn't INVALID —
    genuinely invalid/dead candidates are never selectable (item 7), no
    matter what is_best says."""
    usable = [r for r in rows if r.validation_status != VALIDATION_INVALID]
    if not usable:
        return None
    flagged = [r for r in usable if r.is_best]
    if flagged:
        return min(flagged, key=lambda r: r.rank)
    return min(usable, key=lambda r: r.rank)


def generate_candidates_for_lead(
    lead: Lead,
    *,
    primary_generators: list[CandidateGenerator] | None = None,
    supplementary_generators: list[CandidateGenerator] | None = None,
    fallback_generator: CandidateGenerator | None = None,
    mx_checker: MXChecker | None = None,
    smtp_checker: SMTPChecker | None = None,
    enable_smtp: bool = False,
) -> EmailDiscoveryResult:
    """Run the full Day 5 candidate pipeline for a single Lead: generate ->
    normalize (generators normalize internally) -> dedupe -> MX-validate ->
    (optional) SMTP-check -> score -> rank.

    All dependencies are injectable so tests never touch the network, Node,
    or an optional package that may not be installed; every default is a
    graceful-degradation default (missing vendor script -> no primary
    generator; missing Node -> MX_UNKNOWN for everything; SMTP off unless
    explicitly requested).
    """
    primary_generators = (
        primary_generators if primary_generators is not None else default_primary_generators()
    )
    supplementary_generators = (
        supplementary_generators
        if supplementary_generators is not None
        else default_supplementary_generators()
    )
    fallback_generator = (
        fallback_generator if fallback_generator is not None else default_fallback_generator()
    )
    mx_checker = mx_checker if mx_checker is not None else NodeMXChecker()
    smtp_checker = smtp_checker if smtp_checker is not None else NodeSMTPChecker()

    raw: list[EmailCandidate] = []
    for gen in primary_generators:
        raw.extend(gen.generate(lead))
    for gen in supplementary_generators:
        raw.extend(gen.generate(lead))

    candidates = dedupe_candidates(raw)
    used_fallback = False
    if not candidates and fallback_generator is not None and getattr(fallback_generator, "available", True):
        candidates = dedupe_candidates(fallback_generator.generate(lead))
        used_fallback = bool(candidates)

    if candidates:
        domains = sorted({c.domain for c in candidates if c.domain})
        mx_results = mx_checker.check_domains(domains) if domains else {}
        for c in candidates:
            if c.domain and c.domain in mx_results:
                c.mx_status = mx_results[c.domain]
                c.mx_checked = True

        if enable_smtp:
            checkable = [c.email for c in candidates if c.mx_status != MX_DEAD]
            smtp_results = smtp_checker.check_emails(checkable) if checkable else {}
            for c in candidates:
                if c.email in smtp_results:
                    c.smtp_status = smtp_results[c.email]
                    c.smtp_checked = True

    ranked = rank_candidates(candidates)
    usable = [c for c in ranked if is_usable(c)]
    best = usable[0] if usable else None
    resolved_domain = best.domain if best else (ranked[0].domain if ranked else "")

    return EmailDiscoveryResult(
        lead_id=lead.lead_id,
        candidates=ranked,
        best=best,
        used_fallback=used_fallback,
        resolved_domain=resolved_domain,
    )


# ---------------------------------------------------------------------------
# Stage driver: QUALIFIED -> EMAIL_CANDIDATES_FOUND / EMAIL_NOT_FOUND
# ---------------------------------------------------------------------------


def process_lead_email(store: LeadStore, lead: Lead, **kwargs) -> Lead:
    """Run candidate discovery for one QUALIFIED Lead and persist the
    result: populates email/email_status/email_source/email_confidence (and
    company_domain, if it was still blank) then transitions the Lead to
    EMAIL_CANDIDATES_FOUND or EMAIL_NOT_FOUND.

    Fields are written via store.save() *before* the state transition, so a
    process that dies between the two still leaves a QUALIFIED lead with
    its email fields already populated — safe to resume, since re-running
    this function against the same still-QUALIFIED lead is idempotent
    (deterministic generators produce the same candidates) and simply
    finishes the transition.

    Day 6: the full ranked candidate list (not just the winner) is persisted
    via store.save_candidates() before anything else, so the evidence behind
    the pick — every source, pattern, MX/SMTP status, score, and confidence
    — survives independently of which candidate ends up selected, and the
    validation stage (email_validation.py) can re-select a best candidate
    later purely from what's stored here, without regenerating.
    """
    result = generate_candidates_for_lead(lead, **kwargs)
    store.save_candidates(lead.lead_id, candidates_to_rows(lead.lead_id, result.candidates))

    if not lead.company_domain and result.resolved_domain:
        lead.company_domain = result.resolved_domain

    if result.best is not None:
        lead.email = result.best.email
        lead.email_source = result.best.source
        lead.email_confidence = confidence_label(result.best)
        lead.email_status = EMAIL_STATUS_CANDIDATES_FOUND
        store.save(lead)
        return store.transition(lead.lead_id, PipelineStatus.EMAIL_CANDIDATES_FOUND)

    lead.email_status = EMAIL_STATUS_NOT_FOUND
    lead.email_confidence = "none"
    store.save(lead)
    return store.transition(lead.lead_id, PipelineStatus.EMAIL_NOT_FOUND)


def find_and_score_pending_leads(
    store: LeadStore,
    *,
    campaign_id: str | None = None,
    **kwargs,
) -> dict[str, int]:
    """Process every Lead currently in QUALIFIED for a campaign.

    Resumable by construction, mirroring lead_pipeline.qualify_pending_leads:
    it only ever pulls leads still sitting in QUALIFIED, so if the process
    is interrupted partway through, the next call picks up exactly the
    remaining QUALIFIED leads — leads already moved to
    EMAIL_CANDIDATES_FOUND or EMAIL_NOT_FOUND are never touched or
    reprocessed.
    """
    found = 0
    not_found = 0
    for lead in store.list_by_status(PipelineStatus.QUALIFIED, campaign_id=campaign_id):
        updated = process_lead_email(store, lead, **kwargs)
        if updated.status == PipelineStatus.EMAIL_CANDIDATES_FOUND:
            found += 1
        else:
            not_found += 1
    return {"email_candidates_found": found, "email_not_found": not_found}


if __name__ == "__main__":
    import argparse

    from .config import load_env

    ap = argparse.ArgumentParser(description="Day 5: find email candidates for QUALIFIED leads.")
    ap.add_argument("--db", default=None, help="Path to the LeadStore SQLite file (default: data/pipeline_state.db)")
    ap.add_argument("--campaign-id", default=None, help="Only process leads for this campaign_id")
    ap.add_argument("--enable-smtp", action="store_true", help="Also run SMTP RCPT-TO checks (slow, best-effort)")
    args = ap.parse_args()

    load_env()
    with (LeadStore(args.db) if args.db else LeadStore()) as store:
        stats = find_and_score_pending_leads(store, campaign_id=args.campaign_id, enable_smtp=args.enable_smtp)
        print(f"EMAIL_CANDIDATES_FOUND: {stats['email_candidates_found']}")
        print(f"EMAIL_NOT_FOUND:        {stats['email_not_found']}")
