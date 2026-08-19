"""Day 4: SQLite persistence for Lead records.

This is the piece that makes the pipeline resumable: every Lead is written
to a small SQLite database (one file, no server) keyed by lead_id, with a
dedup lookup on (campaign_id, identity_key) so re-running discovery or any
later stage against the same campaign never creates duplicate records or
loses progress already made.

Deliberately NOT using Postgres/Redis/an ORM/a task queue — a single SQLite
file is the simplest reliable option for a pipeline that runs as a script or
a single backend process, and it gives us transactions and indexed queries
for free via the standard library.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import ROOT
from .models import (
    LEAD_FIELDNAMES,
    Lead,
    PipelineStatus,
    utc_now_iso,
    validate_transition,
)

DEFAULT_DB_PATH = ROOT / "data" / "pipeline_state.db"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS leads (
    {", ".join(f'{name} TEXT NOT NULL DEFAULT ""' for name in LEAD_FIELDNAMES if name != "lead_id")},
    lead_id TEXT PRIMARY KEY
);
CREATE INDEX IF NOT EXISTS idx_leads_campaign_status
    ON leads (campaign_id, pipeline_status);
CREATE INDEX IF NOT EXISTS idx_leads_campaign_identity
    ON leads (campaign_id, identity_key);

-- Day 6: one row per generated EmailCandidate (not just the winner), keyed
-- by lead_id + rank. LeadStore is deliberately kept generic here — it has
-- no knowledge of the EmailCandidate class, only of plain rows shaped by
-- email_discovery.candidate_to_row() — to avoid a circular import (
-- email_discovery.py already imports LeadStore).
CREATE TABLE IF NOT EXISTS email_candidates (
    candidate_id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    email TEXT NOT NULL,
    sources TEXT NOT NULL DEFAULT '[]',
    patterns TEXT NOT NULL DEFAULT '[]',
    domain TEXT NOT NULL DEFAULT '',
    domain_guessed INTEGER NOT NULL DEFAULT 0,
    mx_status TEXT NOT NULL DEFAULT 'UNKNOWN',
    smtp_status TEXT NOT NULL DEFAULT 'NOT_CHECKED',
    mx_checked INTEGER NOT NULL DEFAULT 0,
    smtp_checked INTEGER NOT NULL DEFAULT 0,
    score REAL NOT NULL DEFAULT 0.0,
    confidence TEXT NOT NULL DEFAULT 'none',
    validation_status TEXT NOT NULL DEFAULT 'GENERATED',
    is_best INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_email_candidates_lead_rank
    ON email_candidates (lead_id, rank);

-- Day 7: one row per Campaign. LeadStore stays generic here too (plain
-- dict rows shaped by campaign.Campaign.to_dict()) to avoid campaign.py
-- <-> lead_store.py import cycles, same reasoning as email_candidates
-- above.
CREATE TABLE IF NOT EXISTS campaigns (
    campaign_id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    subject_template TEXT NOT NULL DEFAULT '',
    body_template TEXT NOT NULL DEFAULT '',
    sender_name TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    -- Aug 2026: optional, admin-supplied, VERIFIED case studies/results a
    -- campaign can draw on for outreach copy. JSON list of objects, each
    -- with industries/keywords lists and a text field. "" / "[]"
    -- means none configured -- the generator must never fabricate one when
    -- this is empty (see email_generation.py select_case_study()).
    case_studies_json TEXT NOT NULL DEFAULT '[]',
    -- Aug 2026: optional, admin-supplied, industry-tailored problem
    -- hypotheses (requirement: "make it tailored to any industry"). JSON
    -- list of objects with industries/keywords/roles lists, a label, and
    -- a phrase field. "[]" (the default) means the generator falls
    -- back to its industry-neutral generic pool (see
    -- email_generation.py's _ROLE_PAIN_ANGLES / select_pain_angle()) --
    -- never a hardcoded tech/SaaS-specific one.
    pain_points_json TEXT NOT NULL DEFAULT '[]'
);

-- Day 7: exactly one EmailJob (generated draft) per lead — the exact
-- subject/body that will eventually be sent, persisted so it is never
-- silently regenerated. UNIQUE(lead_id) is what makes save_email_job an
-- upsert-by-lead rather than an ever-growing history table.
CREATE TABLE IF NOT EXISTS email_jobs (
    job_id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL UNIQUE,
    campaign_id TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    review_status TEXT NOT NULL DEFAULT 'PENDING',
    edited INTEGER NOT NULL DEFAULT 0,
    rejection_reason TEXT NOT NULL DEFAULT '',
    generated_at TEXT NOT NULL DEFAULT '',
    reviewed_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    -- Aug 2026: debugging/detail-view metadata (hook_type, evidence_used,
    -- evidence_sources, personalization_confidence, cta_type,
    -- email_quality_score, stage) -- JSON object, empty object if not
    -- computed. Deliberately NOT rendered into the main email UI; exposed
    -- only via the prospect detail endpoint for anyone who wants it.
    metadata_json TEXT NOT NULL DEFAULT '{{}}'
);
CREATE INDEX IF NOT EXISTS idx_email_jobs_campaign_review
    ON email_jobs (campaign_id, review_status);

-- Day 8: one row per lead_id (UNIQUE(lead_id), same convention as
-- email_jobs) tracking the *sending* lifecycle of an already-approved
-- EmailJob: QUEUED -> SENDING -> SENT / SEND_FAILED. Deliberately a
-- separate table from email_jobs rather than new columns bolted onto it --
-- email_jobs is Day 7's draft/review record (subject/body/review_status)
-- and is left untouched; email_sends is purely additive. UNIQUE(lead_id)
-- is what makes queueing idempotent: a lead can only ever have one
-- send-queue row, so re-running the queueing step after a restart can
-- never create a duplicate entry for a lead that's already queued or
-- already sent.
CREATE TABLE IF NOT EXISTS email_sends (
    job_id TEXT PRIMARY KEY,
    lead_id TEXT NOT NULL UNIQUE,
    campaign_id TEXT NOT NULL DEFAULT '',
    to_email TEXT NOT NULL DEFAULT '',
    send_status TEXT NOT NULL DEFAULT 'QUEUED',
    retry_count INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 2,
    provider_message_id TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    queued_at TEXT NOT NULL DEFAULT '',
    sending_started_at TEXT NOT NULL DEFAULT '',
    sent_at TEXT NOT NULL DEFAULT '',
    failed_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_email_sends_campaign_status
    ON email_sends (campaign_id, send_status);

-- Day 9: one row per campaign, holding its run-state (RUNNING/PAUSED/
-- STOPPED) and per-campaign overrides for sending pace/limits/retries.
-- Purely additive, same pattern as campaigns/email_jobs/email_sends above
-- (generic dict rows, no dependency on campaign_control.py, so lead_store
-- never imports it -- avoids an import cycle). A campaign with no row here
-- is simply "RUNNING with module defaults" -- see
-- campaign_control.get_campaign_control().
CREATE TABLE IF NOT EXISTS campaign_controls (
    campaign_id TEXT PRIMARY KEY,
    run_state TEXT NOT NULL DEFAULT 'RUNNING',
    max_per_run TEXT NOT NULL DEFAULT '',
    delay_seconds TEXT NOT NULL DEFAULT '',
    max_retries TEXT NOT NULL DEFAULT '',
    retry_backoff_seconds TEXT NOT NULL DEFAULT '',
    paused_at TEXT NOT NULL DEFAULT '',
    resumed_at TEXT NOT NULL DEFAULT '',
    stopped_at TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT ''
);

-- Day 9: do-not-contact list. email_normalized is the lookup key
-- (lower/stripped); campaign_id = '' means "suppressed everywhere", a
-- non-empty campaign_id scopes the suppression to just that campaign. A
-- given (email_normalized, campaign_id) pair is unique -- suppressing the
-- same address twice for the same scope is an upsert, not a duplicate row.
CREATE TABLE IF NOT EXISTS suppressed_contacts (
    email_normalized TEXT NOT NULL,
    campaign_id TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    added_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (email_normalized, campaign_id)
);
CREATE INDEX IF NOT EXISTS idx_suppressed_contacts_email
    ON suppressed_contacts (email_normalized);
"""

_CANDIDATE_COLUMNS = [
    "candidate_id", "lead_id", "rank", "email", "sources", "patterns",
    "domain", "domain_guessed", "mx_status", "smtp_status", "mx_checked",
    "smtp_checked", "score", "confidence", "validation_status", "is_best",
    "created_at",
]

_CAMPAIGN_COLUMNS = [
    "campaign_id", "name", "description", "subject_template",
    "body_template", "sender_name", "status", "created_at", "updated_at",
    "case_studies_json", "pain_points_json",
]

_EMAIL_JOB_COLUMNS = [
    "job_id", "lead_id", "campaign_id", "subject", "body", "review_status",
    "edited", "rejection_reason", "generated_at", "reviewed_at",
    "created_at", "updated_at", "metadata_json",
]

_EMAIL_SEND_COLUMNS = [
    "job_id", "lead_id", "campaign_id", "to_email", "send_status",
    "retry_count", "max_retries", "provider_message_id", "last_error",
    "queued_at", "sending_started_at", "sent_at", "failed_at",
    "created_at", "updated_at",
]

_CAMPAIGN_CONTROL_COLUMNS = [
    "campaign_id", "run_state", "max_per_run", "delay_seconds",
    "max_retries", "retry_backoff_seconds", "paused_at", "resumed_at",
    "stopped_at", "created_at", "updated_at",
]

_SUPPRESSED_CONTACT_COLUMNS = [
    "email_normalized", "campaign_id", "reason", "added_at",
]


class LeadStore:
    """Thin, explicit SQLite wrapper for Lead persistence.

    One LeadStore per database file. Safe to open the same file from
    multiple runs (e.g. resuming later) — schema creation is idempotent and
    every write is a single committed transaction.
    """

    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        if str(self.db_path) != ":memory:":
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;") if str(self.db_path) != ":memory:" else None
        self._init_schema()

    def _init_schema(self) -> None:
        with self._cursor() as cur:
            cur.executescript(_SCHEMA)
        self._migrate_leads_columns()
        self._migrate_table_columns("campaigns", ["case_studies_json", "pain_points_json"], defaults={"case_studies_json": "[]", "pain_points_json": "[]"})
        self._migrate_table_columns("email_jobs", ["metadata_json"], defaults={"metadata_json": "{}"})

    def _migrate_leads_columns(self) -> None:
        """`CREATE TABLE IF NOT EXISTS` (in _SCHEMA above) is a no-op for a
        `leads` table that already exists from a previous run -- so adding a
        new name to LEAD_FIELDNAMES (e.g. `qualification_evidence`) would
        silently NOT reach an existing database file without this. Adds any
        column present in LEAD_FIELDNAMES but missing from the actual
        on-disk table, defaulting to '' exactly like a fresh CREATE TABLE
        would. Safe to run on every open: no-ops once columns exist.
        """
        with self._cursor() as cur:
            cur.execute("PRAGMA table_info(leads)")
            existing = {row[1] for row in cur.fetchall()}  # row[1] == column name
            for name in LEAD_FIELDNAMES:
                if name not in existing:
                    cur.execute(f'ALTER TABLE leads ADD COLUMN {name} TEXT NOT NULL DEFAULT ""')

    def _migrate_table_columns(
        self, table: str, columns: list[str], *, defaults: dict[str, str] | None = None
    ) -> None:
        """Generic version of _migrate_leads_columns for the hand-written
        (non-FIELDNAMES-derived) tables -- campaigns/email_jobs/etc. Adds
        any of `columns` missing from the on-disk table, each defaulting to
        '' unless overridden in `defaults`. No-ops once columns exist."""
        defaults = defaults or {}
        with self._cursor() as cur:
            cur.execute(f"PRAGMA table_info({table})")
            existing = {row[1] for row in cur.fetchall()}
            for name in columns:
                if name not in existing:
                    default = defaults.get(name, "")
                    escaped = default.replace("'", "''")
                    cur.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} TEXT NOT NULL DEFAULT '{escaped}'"
                    )

    @contextmanager
    def _cursor(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    def close(self) -> None:
        """Cleanly release this store's SQLite connection.

        For a file-backed database, WAL journal mode (enabled in
        __init__ for concurrency) leaves the durable data split across the
        main db file plus `-wal`/`-shm` side files until something
        checkpoints them back together. `sqlite3.Connection.close()` alone
        does not guarantee that merge happens before the OS-level file
        handle is released -- on Windows in particular, that can leave the
        db file (or its side files) transiently locked even after this
        call returns, which breaks any caller that needs to immediately
        delete, move, or otherwise touch the file from outside sqlite3
        (e.g. a temp-file test's cleanup, or restarting a campaign from
        scratch). Switching back to the default rollback-journal mode
        forces SQLite to fully checkpoint the WAL into the main file and
        remove the `-wal`/`-shm` files itself -- via its own file handle,
        which it owns and knows how to release correctly -- rather than
        leaving that race between "this connection reports closed" and
        "the OS has actually released the file" to the caller. This only
        changes the journal mode at shutdown; WAL is still used for the
        store's entire working lifetime, so this has no effect on the
        persistence/concurrency behavior being relied on elsewhere, only
        on how cleanly a *closed* store lets go of its file.
        """
        if str(self.db_path) != ":memory:":
            try:
                self._conn.execute("PRAGMA journal_mode=DELETE;")
            except sqlite3.Error:
                pass
        self._conn.close()

    def __enter__(self) -> "LeadStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- row <-> Lead -------------------------------------------------------

    @staticmethod
    def _row_to_lead(row: sqlite3.Row) -> Lead:
        return Lead.from_dict({k: row[k] for k in row.keys()})

    # -- create / read --------------------------------------------------

    def _insert(self, lead: Lead) -> None:
        data = lead.to_dict()
        cols = list(data.keys())
        placeholders = ", ".join("?" for _ in cols)
        with self._cursor() as cur:
            cur.execute(
                f"INSERT INTO leads ({', '.join(cols)}) VALUES ({placeholders})",
                [data[c] for c in cols],
            )

    def _update(self, lead: Lead) -> None:
        data = lead.to_dict()
        cols = [c for c in data.keys() if c != "lead_id"]
        set_clause = ", ".join(f"{c} = ?" for c in cols)
        with self._cursor() as cur:
            cur.execute(
                f"UPDATE leads SET {set_clause} WHERE lead_id = ?",
                [data[c] for c in cols] + [lead.lead_id],
            )

    def get(self, lead_id: str) -> Lead | None:
        cur = self._conn.execute("SELECT * FROM leads WHERE lead_id = ?", (lead_id,))
        row = cur.fetchone()
        return self._row_to_lead(row) if row else None

    def get_by_identity_key(self, campaign_id: str, identity_key: str) -> Lead | None:
        if not identity_key:
            return None
        cur = self._conn.execute(
            "SELECT * FROM leads WHERE campaign_id = ? AND identity_key = ?",
            (campaign_id, identity_key),
        )
        row = cur.fetchone()
        return self._row_to_lead(row) if row else None

    def list_by_status(
        self,
        status: PipelineStatus | str,
        *,
        campaign_id: str | None = None,
        limit: int | None = None,
    ) -> list[Lead]:
        status_value = PipelineStatus(status).value if not isinstance(status, PipelineStatus) else status.value
        query = "SELECT * FROM leads WHERE pipeline_status = ?"
        params: list[str] = [status_value]
        if campaign_id is not None:
            query += " AND campaign_id = ?"
            params.append(campaign_id)
        query += " ORDER BY created_at ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(str(int(limit)))
        cur = self._conn.execute(query, params)
        return [self._row_to_lead(r) for r in cur.fetchall()]

    def all(self, *, campaign_id: str | None = None) -> list[Lead]:
        if campaign_id is not None:
            cur = self._conn.execute(
                "SELECT * FROM leads WHERE campaign_id = ? ORDER BY created_at ASC",
                (campaign_id,),
            )
        else:
            cur = self._conn.execute("SELECT * FROM leads ORDER BY created_at ASC")
        return [self._row_to_lead(r) for r in cur.fetchall()]

    def count_by_status(self, *, campaign_id: str | None = None) -> dict[str, int]:
        """Counts per pipeline_status — the resumability snapshot, e.g.
        {"DISCOVERED": 1000, "QUALIFIED": 900, "EMAIL_CANDIDATES_FOUND": 700,
         "EMAIL_VALIDATED": 650, ...}.
        """
        query = "SELECT pipeline_status, COUNT(*) AS n FROM leads"
        params: list[str] = []
        if campaign_id is not None:
            query += " WHERE campaign_id = ?"
            params.append(campaign_id)
        query += " GROUP BY pipeline_status"
        cur = self._conn.execute(query, params)
        return {row["pipeline_status"]: row["n"] for row in cur.fetchall()}

    # -- write: dedup upsert -------------------------------------------------

    def upsert_lead(self, lead: Lead) -> tuple[Lead, bool]:
        """Insert a new Lead, or merge into an existing one with the same
        (campaign_id, identity_key).

        Returns (stored_lead, created). On merge:
          - the existing lead_id and pipeline_status are preserved (a
            re-ingested lead is never reset back to DISCOVERED, and never
            silently regressed — this is what makes ingestion resumable);
          - blank fields on the existing record are filled in from the new
            data;
          - non-blank fields are left as-is (first write wins), except
            updated_at which always advances.

        A Lead with no identity_key (identity_key == "") is never deduped —
        it is always inserted as a new record, since we have no reliable way
        to know it's the same person. See lead_pipeline.compute_identity_key.
        """
        existing = (
            self.get_by_identity_key(lead.campaign_id, lead.identity_key)
            if lead.identity_key
            else None
        )
        if existing is None:
            self._insert(lead)
            return lead, True

        merged_data = existing.to_dict()
        new_data = lead.to_dict()
        for key, value in new_data.items():
            if key in ("lead_id", "pipeline_status", "created_at", "identity_key"):
                continue
            if not merged_data.get(key) and value:
                merged_data[key] = value
        merged_data["updated_at"] = utc_now_iso()
        merged = Lead.from_dict(merged_data)
        self._update(merged)
        return merged, False

    # -- write: field updates (no status change) -----------------------------

    def save(self, lead: Lead) -> Lead:
        """Persist a Lead's current field values as-is.

        Unlike `transition`, this does NOT touch or validate pipeline_status
        — it's for stages (Day 5+) that need to write non-status fields
        (e.g. email, email_source, company_domain) discovered for a lead
        that is not itself changing pipeline stage yet, or immediately
        before a subsequent `transition()` call. Callers that also need to
        move `pipeline_status` should still go through `transition()` so the
        state machine boundary in models.py is always the one place that
        decides whether a status change is legal; this method never changes
        pipeline_status itself (it just writes back whatever value is
        already on the given Lead).
        """
        lead.updated_at = utc_now_iso()
        self._update(lead)
        return lead

    # -- write: validated state transitions ----------------------------------

    def transition(self, lead_id: str, new_status: PipelineStatus | str) -> Lead:
        """Move a Lead to `new_status`, enforcing the pipeline state machine.

        Raises KeyError if the lead doesn't exist, InvalidStateTransition
        (from models.py) if the move isn't legal from the lead's current
        status.
        """
        lead = self.get(lead_id)
        if lead is None:
            raise KeyError(f"No lead with lead_id={lead_id!r}")
        target = validate_transition(lead.pipeline_status, new_status)
        lead.pipeline_status = target.value
        lead.updated_at = utc_now_iso()
        self._update(lead)
        return lead

    # -- Day 6: candidate persistence ---------------------------------------
    #
    # Generic row storage — no dependency on email_discovery.EmailCandidate.
    # Callers pass/receive plain dicts shaped like
    # email_discovery.candidate_to_row()'s output.

    def save_candidates(self, lead_id: str, rows: list[dict]) -> None:
        """Replace the full persisted candidate set for one lead.

        Full replace-on-write (delete-then-insert in one transaction)
        rather than an upsert: candidate generation is deterministic and
        re-running it for a lead that's still QUALIFIED is expected to be
        idempotent (see process_lead_email's docstring), so the simplest
        correct behavior is "the persisted set always matches the most
        recent generation run" — there's no notion of merging two
        generation attempts' candidates together.
        """
        with self._cursor() as cur:
            cur.execute("DELETE FROM email_candidates WHERE lead_id = ?", (lead_id,))
            if rows:
                placeholders = ", ".join("?" for _ in _CANDIDATE_COLUMNS)
                cur.executemany(
                    f"INSERT INTO email_candidates ({', '.join(_CANDIDATE_COLUMNS)}) "
                    f"VALUES ({placeholders})",
                    [[row[c] for c in _CANDIDATE_COLUMNS] for row in rows],
                )

    def list_candidates(self, lead_id: str) -> list[dict]:
        """All persisted candidates for one lead, best-first (ascending
        rank) — exactly the order needed to re-select a best candidate
        without recomputing anything."""
        cur = self._conn.execute(
            "SELECT * FROM email_candidates WHERE lead_id = ? ORDER BY rank ASC",
            (lead_id,),
        )
        return [{k: row[k] for k in row.keys()} for row in cur.fetchall()]

    def clear_candidates(self, lead_id: str) -> None:
        with self._cursor() as cur:
            cur.execute("DELETE FROM email_candidates WHERE lead_id = ?", (lead_id,))

    # -- Day 7: campaign persistence -----------------------------------------
    #
    # Generic row storage again — no dependency on campaign.Campaign, same
    # pattern as the candidate methods above.

    def save_campaign(self, row: dict) -> None:
        """Insert a new Campaign row, or update it in place if campaign_id
        already exists (idempotent upsert by primary key)."""
        cols = _CAMPAIGN_COLUMNS
        placeholders = ", ".join("?" for _ in cols)
        assignments = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "campaign_id")
        with self._cursor() as cur:
            cur.execute(
                f"INSERT INTO campaigns ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(campaign_id) DO UPDATE SET {assignments}",
                [row.get(c, "") for c in cols],
            )

    def get_campaign(self, campaign_id: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM campaigns WHERE campaign_id = ?", (campaign_id,)
        )
        row = cur.fetchone()
        return {k: row[k] for k in row.keys()} if row else None

    def list_campaigns(self) -> list[dict]:
        cur = self._conn.execute("SELECT * FROM campaigns ORDER BY created_at ASC")
        return [{k: row[k] for k in row.keys()} for row in cur.fetchall()]

    # -- Day 7: EmailJob (generated draft) persistence -----------------------
    #
    # One row per lead_id (see UNIQUE(lead_id) in the schema): the exact
    # subject/body that will eventually be sent, so it is never silently
    # regenerated once a lead reaches EMAIL_GENERATED.

    def save_email_job(self, row: dict) -> None:
        """Insert a new EmailJob row, or overwrite the existing draft for
        this lead_id in place (upsert by lead_id, not job_id — there is
        only ever one current draft per lead)."""
        cols = _EMAIL_JOB_COLUMNS
        placeholders = ", ".join("?" for _ in cols)
        assignments = ", ".join(
            f"{c} = excluded.{c}" for c in cols if c not in ("job_id", "lead_id")
        )
        with self._cursor() as cur:
            cur.execute(
                f"INSERT INTO email_jobs ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(lead_id) DO UPDATE SET {assignments}",
                [row.get(c, "") for c in cols],
            )

    def get_email_job(self, lead_id: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM email_jobs WHERE lead_id = ?", (lead_id,)
        )
        row = cur.fetchone()
        return {k: row[k] for k in row.keys()} if row else None

    def list_email_jobs(
        self,
        *,
        campaign_id: str | None = None,
        review_status: str | None = None,
    ) -> list[dict]:
        query = "SELECT * FROM email_jobs WHERE 1 = 1"
        params: list[str] = []
        if campaign_id is not None:
            query += " AND campaign_id = ?"
            params.append(campaign_id)
        if review_status is not None:
            query += " AND review_status = ?"
            params.append(review_status)
        query += " ORDER BY created_at ASC"
        cur = self._conn.execute(query, params)
        return [{k: row[k] for k in row.keys()} for row in cur.fetchall()]

    # -- Day 8: EmailSend (sending-queue) persistence -------------------------
    #
    # Generic row storage again — no dependency on email_sending.EmailSend,
    # same pattern as campaigns/email_jobs above. UNIQUE(lead_id) on the
    # table is what makes save_email_send an upsert-by-lead: queueing the
    # same lead twice (e.g. after a restart) overwrites the same row rather
    # than creating a second one.

    def save_email_send(self, row: dict) -> None:
        """Insert a new EmailSend row, or update the existing one for this
        lead_id in place (upsert by lead_id, not job_id)."""
        cols = _EMAIL_SEND_COLUMNS
        placeholders = ", ".join("?" for _ in cols)
        assignments = ", ".join(
            f"{c} = excluded.{c}" for c in cols if c not in ("job_id", "lead_id")
        )
        with self._cursor() as cur:
            cur.execute(
                f"INSERT INTO email_sends ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(lead_id) DO UPDATE SET {assignments}",
                [row.get(c, "") for c in cols],
            )

    def get_email_send(self, lead_id: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM email_sends WHERE lead_id = ?", (lead_id,)
        )
        row = cur.fetchone()
        return {k: row[k] for k in row.keys()} if row else None

    def list_email_sends(
        self,
        *,
        campaign_id: str | None = None,
        send_status: str | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        query = "SELECT * FROM email_sends WHERE 1 = 1"
        params: list[str] = []
        if campaign_id is not None:
            query += " AND campaign_id = ?"
            params.append(campaign_id)
        if send_status is not None:
            query += " AND send_status = ?"
            params.append(send_status)
        query += " ORDER BY queued_at ASC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(str(int(limit)))
        cur = self._conn.execute(query, params)
        return [{k: row[k] for k in row.keys()} for row in cur.fetchall()]

    def find_email_sends_by_email(
        self, to_email: str, *, send_status: str | None = None
    ) -> list[dict]:
        """All EmailSend rows for a given recipient address, across every
        lead_id/campaign -- used for duplicate-send protection (the same
        address discovered under two different lead_ids should still only
        ever be emailed once).
        """
        query = "SELECT * FROM email_sends WHERE LOWER(to_email) = LOWER(?)"
        params: list[str] = [to_email]
        if send_status is not None:
            query += " AND send_status = ?"
            params.append(send_status)
        query += " ORDER BY created_at ASC"
        cur = self._conn.execute(query, params)
        return [{k: row[k] for k in row.keys()} for row in cur.fetchall()]

    # -- Day 9: campaign run-state + sending-control persistence -------------

    def save_campaign_control(self, row: dict) -> None:
        """Insert a new CampaignControl row, or update it in place if
        campaign_id already exists (idempotent upsert by primary key)."""
        cols = _CAMPAIGN_CONTROL_COLUMNS
        placeholders = ", ".join("?" for _ in cols)
        assignments = ", ".join(f"{c} = excluded.{c}" for c in cols if c != "campaign_id")
        with self._cursor() as cur:
            cur.execute(
                f"INSERT INTO campaign_controls ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(campaign_id) DO UPDATE SET {assignments}",
                [row.get(c, "") for c in cols],
            )

    def get_campaign_control(self, campaign_id: str) -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM campaign_controls WHERE campaign_id = ?", (campaign_id,)
        )
        row = cur.fetchone()
        return {k: row[k] for k in row.keys()} if row else None

    # -- Day 9: suppression / do-not-contact list persistence ----------------

    def save_suppressed_contact(self, row: dict) -> None:
        """Insert a new suppression row, or update it in place if the
        (email_normalized, campaign_id) pair already exists."""
        cols = _SUPPRESSED_CONTACT_COLUMNS
        placeholders = ", ".join("?" for _ in cols)
        assignments = ", ".join(
            f"{c} = excluded.{c}" for c in cols if c not in ("email_normalized", "campaign_id")
        )
        with self._cursor() as cur:
            cur.execute(
                f"INSERT INTO suppressed_contacts ({', '.join(cols)}) VALUES ({placeholders}) "
                f"ON CONFLICT(email_normalized, campaign_id) DO UPDATE SET {assignments}",
                [row.get(c, "") for c in cols],
            )

    def get_suppressed_contact(self, email_normalized: str, campaign_id: str = "") -> dict | None:
        cur = self._conn.execute(
            "SELECT * FROM suppressed_contacts WHERE email_normalized = ? AND campaign_id = ?",
            (email_normalized, campaign_id),
        )
        row = cur.fetchone()
        return {k: row[k] for k in row.keys()} if row else None

    def list_suppressed_contacts(self, *, campaign_id: str | None = None) -> list[dict]:
        if campaign_id is not None:
            cur = self._conn.execute(
                "SELECT * FROM suppressed_contacts WHERE campaign_id IN (?, '') "
                "ORDER BY added_at ASC",
                (campaign_id,),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM suppressed_contacts ORDER BY added_at ASC"
            )
        return [{k: row[k] for k in row.keys()} for row in cur.fetchall()]

    def delete_suppressed_contact(self, email_normalized: str, campaign_id: str = "") -> None:
        with self._cursor() as cur:
            cur.execute(
                "DELETE FROM suppressed_contacts WHERE email_normalized = ? AND campaign_id = ?",
                (email_normalized, campaign_id),
            )
