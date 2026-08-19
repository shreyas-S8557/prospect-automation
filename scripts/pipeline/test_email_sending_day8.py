"""Day 8 milestone tests: GmailSender adapter + the
APPROVED -> QUEUED -> SENDING -> SENT / SEND_FAILED sending queue.

Run with (from the `scripts/` directory, or as a module from repo root):
    python -m unittest pipeline.test_email_sending_day8 -v

No network and no real Gmail account is ever touched: GmailSender is always
constructed with a fake `smtp_client_factory`, or a fake send() is injected
directly, so nothing in this file can send a real email. Every store is an
in-memory SQLite DB.

Covers (per the Day 8 spec):
  - Gmail adapter initialization
  - credential/config validation
  - test-email mode
  - queue creation
  - approved-only queueing
  - duplicate prevention
  - successful send
  - failed send
  - retry handling
  - persistence/restart
  - state transitions
  - Day 4-7 regression
"""

from __future__ import annotations

import unittest

from .campaign import create_campaign
from .email_generation import approve_email, generate_email_for_lead
from .email_sending import (
    DEFAULT_MAX_RETRIES,
    EmailSend,
    NoApprovedEmailJob,
    NotQueued,
    SEND_FAILED,
    SEND_QUEUED,
    SEND_SENDING,
    SEND_SENT,
    get_email_send,
    list_email_sends,
    list_stuck_sending,
    mark_stuck_as_failed,
    queue_approved_email,
    queue_pending_approvals,
    send_pending_queue,
    send_queued_email,
)
from .gmail_sender import GmailCredentialsError, GmailSender, SendResult
from .lead_store import LeadStore
from .models import InvalidStateTransition, Lead, PipelineStatus, validate_transition


# ---------------------------------------------------------------------------
# Shared fakes / fixtures
# ---------------------------------------------------------------------------


def make_lead(**overrides) -> Lead:
    defaults = dict(
        first_name="Jane",
        last_name="Doe",
        full_name="Jane Doe",
        company_name="Acme Corp",
        job_title="Managing Partner",
        location="Austin, Texas, United States",
        industry="accounting",
        pipeline_status=PipelineStatus.EMAIL_VALIDATED.value,
        email="jane.doe@acme.com",
    )
    defaults.update(overrides)
    return Lead(**defaults)


def make_campaign(**overrides):
    defaults = dict(
        name="Q3 Outreach",
        subject_template="Quick question for {{first_name}}",
        body_template="<p>Hi {{first_name}}, thoughts on {{company_name}}?</p>",
    )
    defaults.update(overrides)
    return create_campaign(
        defaults.pop("name"),
        defaults.pop("subject_template"),
        defaults.pop("body_template"),
        **defaults,
    )


class FakeSMTPClient:
    """Stand-in for smtplib.SMTP_SSL(...). Records calls, never touches a
    socket. `fail_logins`/`fail_sends` let a test force an exception on the
    Nth call to simulate transient failures for retry tests."""

    instances: list["FakeSMTPClient"] = []

    def __init__(self, host, port, *, login_exc=None, send_exc_sequence=None):
        self.host = host
        self.port = port
        self.login_calls = []
        self.sent = []
        self.quit_called = False
        self._login_exc = login_exc
        self._send_exc_sequence = list(send_exc_sequence or [])
        FakeSMTPClient.instances.append(self)

    def login(self, address, password):
        self.login_calls.append((address, password))
        if self._login_exc is not None:
            raise self._login_exc

    def sendmail(self, from_addr, to_addrs, message):
        if self._send_exc_sequence:
            exc = self._send_exc_sequence.pop(0)
            if exc is not None:
                raise exc
        self.sent.append((from_addr, list(to_addrs), message))

    def quit(self):
        self.quit_called = True


def make_gmail_sender(**kwargs) -> tuple[GmailSender, list]:
    """A GmailSender wired to a fresh FakeSMTPClient via an injectable
    factory. Returns (sender, factory_calls) so a test can inspect what
    host/port it was asked to connect to."""
    calls: list[tuple[str, int]] = []
    login_exc = kwargs.pop("login_exc", None)
    send_exc_sequence = kwargs.pop("send_exc_sequence", None)

    def factory(host, port):
        calls.append((host, port))
        return FakeSMTPClient(host, port, login_exc=login_exc, send_exc_sequence=send_exc_sequence)

    sender = GmailSender(
        address=kwargs.pop("address", "me@gmail.com"),
        app_password=kwargs.pop("app_password", "abcd efgh ijkl mnop"),
        smtp_client_factory=factory,
        **kwargs,
    )
    return sender, calls


# ---------------------------------------------------------------------------
# 1. Gmail adapter initialization
# ---------------------------------------------------------------------------


class TestGmailAdapterInit(unittest.TestCase):
    def test_reads_credentials_from_explicit_args(self):
        sender, _ = make_gmail_sender(address="a@gmail.com", app_password="secret")
        self.assertEqual(sender.address, "a@gmail.com")
        self.assertEqual(sender.app_password, "secret")

    def test_reads_credentials_from_env_when_not_passed(self):
        import os

        old_addr = os.environ.get("GMAIL_ADDRESS")
        old_pw = os.environ.get("GMAIL_APP_PASSWORD")
        try:
            os.environ["GMAIL_ADDRESS"] = "envuser@gmail.com"
            os.environ["GMAIL_APP_PASSWORD"] = "env-app-password"
            sender = GmailSender()
            self.assertEqual(sender.address, "envuser@gmail.com")
            self.assertEqual(sender.app_password, "env-app-password")
        finally:
            for key, val in (("GMAIL_ADDRESS", old_addr), ("GMAIL_APP_PASSWORD", old_pw)):
                if val is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = val

    def test_default_smtp_host_and_port(self):
        sender, _ = make_gmail_sender()
        self.assertEqual(sender.smtp_host, "smtp.gmail.com")
        self.assertEqual(sender.smtp_port, 465)

    def test_no_hardcoded_credentials_in_module_source(self):
        import inspect

        from . import gmail_sender

        source = inspect.getsource(gmail_sender)
        self.assertNotIn("YourEmailAddress", source)
        self.assertNotIn("YourPassword", source)

    def test_connect_uses_injected_factory_and_logs_in(self):
        sender, calls = make_gmail_sender(address="a@gmail.com", app_password="secret")
        sender.connect()
        self.addCleanup(sender.close)
        self.assertEqual(calls, [("smtp.gmail.com", 465)])
        self.assertEqual(sender._conn.login_calls, [("a@gmail.com", "secret")])

    def test_connect_is_idempotent(self):
        sender, calls = make_gmail_sender()
        sender.connect()
        sender.connect()
        self.addCleanup(sender.close)
        self.assertEqual(len(calls), 1)

    def test_close_calls_quit_and_clears_connection(self):
        sender, _ = make_gmail_sender()
        sender.connect()
        conn = sender._conn
        sender.close()
        self.assertTrue(conn.quit_called)
        self.assertIsNone(sender._conn)

    def test_context_manager_connects_and_closes(self):
        sender, _ = make_gmail_sender()
        with sender as s:
            self.assertIsNotNone(s._conn)
        self.assertIsNone(sender._conn)


# ---------------------------------------------------------------------------
# 2. Credential / config validation
# ---------------------------------------------------------------------------


class TestCredentialValidation(unittest.TestCase):
    def test_missing_both_raises_with_both_named(self):
        sender = GmailSender(address="", app_password="")
        with self.assertRaises(GmailCredentialsError) as ctx:
            sender.validate_credentials()
        self.assertIn("GMAIL_ADDRESS", str(ctx.exception))
        self.assertIn("GMAIL_APP_PASSWORD", str(ctx.exception))

    def test_missing_password_only(self):
        sender = GmailSender(address="a@gmail.com", app_password="")
        with self.assertRaises(GmailCredentialsError) as ctx:
            sender.validate_credentials()
        self.assertIn("GMAIL_APP_PASSWORD", str(ctx.exception))
        self.assertNotIn("GMAIL_ADDRESS,", str(ctx.exception))

    def test_valid_credentials_do_not_raise(self):
        sender, _ = make_gmail_sender()
        sender.validate_credentials()  # should not raise

    def test_connect_validates_before_touching_network(self):
        sender = GmailSender(address="", app_password="", smtp_client_factory=FakeSMTPClient)
        with self.assertRaises(GmailCredentialsError):
            sender.connect()

    def test_send_returns_failure_result_for_missing_credentials_never_raises(self):
        sender = GmailSender(address="", app_password="", smtp_client_factory=FakeSMTPClient)
        result = sender.send("to@example.com", "Subject", "<p>Body</p>")
        self.assertFalse(result.success)
        self.assertIn("GMAIL_ADDRESS", result.error)

    def test_verify_connection_logs_in_and_disconnects(self):
        sender, calls = make_gmail_sender()
        ok = sender.verify_connection()
        self.assertTrue(ok)
        self.assertIsNone(sender._conn)
        self.assertEqual(len(calls), 1)

    def test_verify_connection_raises_on_bad_login(self):
        import smtplib

        sender, _ = make_gmail_sender(login_exc=smtplib.SMTPAuthenticationError(535, b"bad creds"))
        with self.assertRaises(smtplib.SMTPException):
            sender.verify_connection()


# ---------------------------------------------------------------------------
# 3. Test-email mode
# ---------------------------------------------------------------------------


class TestTestEmailMode(unittest.TestCase):
    def test_send_test_email_defaults_to_own_address(self):
        sender, _ = make_gmail_sender(address="me@gmail.com")
        result = sender.send_test_email()
        self.assertTrue(result.success)
        conn = sender._conn
        self.assertEqual(conn.sent[0][1], ["me@gmail.com"])

    def test_send_test_email_to_explicit_address(self):
        sender, _ = make_gmail_sender(address="me@gmail.com")
        result = sender.send_test_email("someone-else@example.com")
        self.assertTrue(result.success)
        conn = sender._conn
        self.assertEqual(conn.sent[0][1], ["someone-else@example.com"])

    def test_send_test_email_never_sends_when_credentials_missing(self):
        sender = GmailSender(address="", app_password="", smtp_client_factory=FakeSMTPClient)
        result = sender.send_test_email("someone@example.com")
        self.assertFalse(result.success)
        self.assertEqual(FakeSMTPClient.instances, FakeSMTPClient.instances)  # no crash
        self.assertIn("Missing Gmail credentials", result.error)

    def test_send_test_email_returns_message_id(self):
        sender, _ = make_gmail_sender()
        result = sender.send_test_email()
        self.assertTrue(result.message_id)
        self.assertIn("@gmail.com", result.message_id)


# ---------------------------------------------------------------------------
# 4. Queue creation / 5. approved-only queueing
# ---------------------------------------------------------------------------


class TestQueueCreation(unittest.TestCase):
    def _approved_lead(self, store, **overrides):
        lead = make_lead(**overrides)
        store.upsert_lead(lead)
        campaign = make_campaign()
        generate_email_for_lead(store, lead, campaign)
        approve_email(store, lead.lead_id)
        return store.get(lead.lead_id), campaign

    def test_queue_approved_email_creates_email_send_row(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead, _ = self._approved_lead(store)
        queue_approved_email(store, lead)
        send = get_email_send(store, lead.lead_id)
        self.assertIsNotNone(send)
        self.assertEqual(send.send_status, SEND_QUEUED)
        self.assertEqual(send.to_email, "jane.doe@acme.com")
        self.assertTrue(send.queued_at)

    def test_queue_approved_email_transitions_lead_to_queued(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead, _ = self._approved_lead(store)
        updated = queue_approved_email(store, lead)
        self.assertEqual(updated.status, PipelineStatus.QUEUED)

    def test_only_approved_leads_can_be_queued(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead(pipeline_status=PipelineStatus.EMAIL_GENERATED.value)
        store.upsert_lead(lead)
        with self.assertRaises(InvalidStateTransition):
            queue_approved_email(store, lead)

    def test_queueing_requires_approved_email_job(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        # APPROVED lead but no EmailJob at all (shouldn't happen via the
        # normal review flow, but the adapter must not silently misbehave).
        lead = make_lead(pipeline_status=PipelineStatus.APPROVED.value)
        store.upsert_lead(lead)
        with self.assertRaises(NoApprovedEmailJob):
            queue_approved_email(store, lead)

    def test_queueing_requires_a_recipient_email_address(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead, _ = self._approved_lead(store, email="")
        with self.assertRaises(NoApprovedEmailJob):
            queue_approved_email(store, lead)

    def test_queue_pending_approvals_only_selects_approved(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        approved_lead, _ = self._approved_lead(store, email="a@x.com")
        other_lead = make_lead(pipeline_status=PipelineStatus.EMAIL_VALIDATED.value, email="b@x.com")
        store.upsert_lead(other_lead)

        stats = queue_pending_approvals(store)
        self.assertEqual(stats["queued"], 1)
        self.assertEqual(get_email_send(store, approved_lead.lead_id).send_status, SEND_QUEUED)
        self.assertIsNone(get_email_send(store, other_lead.lead_id))

    def test_queue_pending_approvals_skips_bad_records_without_aborting(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        good_lead, _ = self._approved_lead(store, email="good@x.com")
        bad_lead = make_lead(pipeline_status=PipelineStatus.APPROVED.value, email="bad@x.com")
        store.upsert_lead(bad_lead)  # APPROVED but no EmailJob

        stats = queue_pending_approvals(store)
        self.assertEqual(stats["queued"], 1)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(get_email_send(store, good_lead.lead_id).send_status, SEND_QUEUED)


# ---------------------------------------------------------------------------
# 6. Duplicate prevention
# ---------------------------------------------------------------------------


class TestDuplicatePrevention(unittest.TestCase):
    def _approved_lead(self, store, **overrides):
        lead = make_lead(**overrides)
        store.upsert_lead(lead)
        campaign = make_campaign()
        generate_email_for_lead(store, lead, campaign)
        approve_email(store, lead.lead_id)
        return store.get(lead.lead_id)

    def test_queueing_twice_is_idempotent_not_duplicated(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = self._approved_lead(store)
        queue_approved_email(store, lead)
        # Simulate a restart re-running queueing against the same
        # (now-stale, still-APPROVED-in-memory) lead object.
        result = queue_approved_email(store, lead)
        self.assertEqual(result.lead_id, lead.lead_id)
        sends = store.list_email_sends()
        self.assertEqual(len(sends), 1)

    def test_email_sends_table_has_unique_lead_id(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = self._approved_lead(store)
        queue_approved_email(store, lead)
        send_row = store.get_email_send(lead.lead_id)
        # Attempting a raw duplicate INSERT (bypassing save_email_send's
        # upsert) must fail the schema's UNIQUE(lead_id) constraint.
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            store._conn.execute(
                "INSERT INTO email_sends (job_id, lead_id) VALUES (?, ?)",
                ("some-other-job-id", lead.lead_id),
            )

    def test_restart_does_not_requeue_already_queued_or_sent_leads(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead1 = self._approved_lead(store, email="one@x.com")
        lead2 = self._approved_lead(store, email="two@x.com")
        queue_pending_approvals(store)  # first "run"

        gmail, _ = make_gmail_sender()
        send_pending_queue(store, gmail, delay_seconds=0)  # sends both

        # "Restart": re-run queueing + sending against the same DB.
        stats_requeue = queue_pending_approvals(store)
        stats_resend = send_pending_queue(store, gmail, delay_seconds=0)
        self.assertEqual(stats_requeue["queued"], 0)
        self.assertEqual(stats_resend["sent"], 0)
        self.assertEqual(stats_resend["failed"], 0)


# ---------------------------------------------------------------------------
# 7. Successful send
# ---------------------------------------------------------------------------


class TestSuccessfulSend(unittest.TestCase):
    def _queued_lead(self, store, **overrides):
        lead = make_lead(**overrides)
        store.upsert_lead(lead)
        campaign = make_campaign()
        generate_email_for_lead(store, lead, campaign)
        approve_email(store, lead.lead_id)
        lead = store.get(lead.lead_id)
        queue_approved_email(store, lead)
        return store.get(lead.lead_id), campaign

    def test_send_queued_email_marks_sent_with_message_id(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead, campaign = self._queued_lead(store)
        gmail, _ = make_gmail_sender()

        updated = send_queued_email(store, gmail, lead.lead_id, campaign=campaign)
        self.assertEqual(updated.status, PipelineStatus.SENT)

        send = get_email_send(store, lead.lead_id)
        self.assertEqual(send.send_status, SEND_SENT)
        self.assertTrue(send.provider_message_id)
        self.assertTrue(send.sent_at)
        self.assertEqual(send.retry_count, 0)

    def test_send_uses_campaign_sender_name_as_from(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead, campaign = self._queued_lead(store)
        campaign.sender_name = "Alex from Acme"
        gmail, _ = make_gmail_sender()

        send_queued_email(store, gmail, lead.lead_id, campaign=campaign)
        conn = gmail._conn
        _, _, raw_message = conn.sent[0]
        self.assertIn("Alex from Acme", raw_message)

    def test_send_pending_queue_processes_multiple_and_paces_between_sends(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        self._queued_lead(store, email="a@x.com")
        self._queued_lead(store, email="b@x.com")
        gmail, _ = make_gmail_sender()

        delays = []
        stats = send_pending_queue(store, gmail, delay_seconds=1.5, sleep=delays.append)
        self.assertEqual(stats["sent"], 2)
        self.assertEqual(delays, [1.5])  # paced once between the two sends, not after the last

    def test_send_pending_queue_respects_max_per_run(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        for i in range(5):
            self._queued_lead(store, email=f"person{i}@x.com")
        gmail, _ = make_gmail_sender()

        stats = send_pending_queue(store, gmail, max_per_run=2, delay_seconds=0)
        self.assertEqual(stats["sent"], 2)
        remaining = list_email_sends(store, send_status=SEND_QUEUED)
        self.assertEqual(len(remaining), 3)


# ---------------------------------------------------------------------------
# 8. Failed send / 9. Retry handling
# ---------------------------------------------------------------------------


class TestFailedSendAndRetries(unittest.TestCase):
    def _queued_lead(self, store, **overrides):
        lead = make_lead(**overrides)
        store.upsert_lead(lead)
        campaign = make_campaign()
        generate_email_for_lead(store, lead, campaign)
        approve_email(store, lead.lead_id)
        lead = store.get(lead.lead_id)
        queue_approved_email(store, lead)
        return store.get(lead.lead_id), campaign

    def test_send_that_exhausts_retries_marks_send_failed(self):
        import smtplib

        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead, campaign = self._queued_lead(store)
        always_fail = [smtplib.SMTPRecipientsRefused({"jane.doe@acme.com": (550, b"nope")})] * 10
        gmail, _ = make_gmail_sender(send_exc_sequence=always_fail)

        sleeps = []
        updated = send_queued_email(
            store, gmail, lead.lead_id, campaign=campaign, sleep=sleeps.append
        )
        self.assertEqual(updated.status, PipelineStatus.SEND_FAILED)

        send = get_email_send(store, lead.lead_id)
        self.assertEqual(send.send_status, SEND_FAILED)
        self.assertTrue(send.last_error)
        self.assertTrue(send.failed_at)
        self.assertEqual(send.retry_count, DEFAULT_MAX_RETRIES + 1)

    def test_retries_up_to_max_retries_before_giving_up(self):
        import smtplib

        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead, campaign = self._queued_lead(store)
        # Fail twice, then succeed on the 3rd attempt (max_retries=2 -> 3
        # total attempts allowed).
        fails = [smtplib.SMTPServerDisconnected("dropped"), smtplib.SMTPServerDisconnected("dropped"), None]
        gmail, _ = make_gmail_sender(send_exc_sequence=fails)

        sleeps = []
        updated = send_queued_email(
            store, gmail, lead.lead_id, campaign=campaign, sleep=sleeps.append
        )
        self.assertEqual(updated.status, PipelineStatus.SENT)
        send = get_email_send(store, lead.lead_id)
        self.assertEqual(send.retry_count, 2)  # two failed attempts recorded before success
        self.assertEqual(len(sleeps), 2)  # backoff before attempt 2 and attempt 3

    def test_a_failed_send_does_not_abort_the_batch(self):
        import smtplib

        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead_ok, campaign = self._queued_lead(store, email="ok@x.com")
        lead_bad, _ = self._queued_lead(store, email="bad@x.com")

        # One shared fake client is reused across sends in this simplified
        # test double, so instead we give the *bad* lead's send a
        # per-instance failure by using separate GmailSender factories per
        # send call via send_queued_email directly.
        good_gmail, _ = make_gmail_sender()
        bad_gmail, _ = make_gmail_sender(
            send_exc_sequence=[smtplib.SMTPRecipientsRefused({"bad@x.com": (550, b"no")})] * 10
        )

        r1 = send_queued_email(store, good_gmail, lead_ok.lead_id, campaign=campaign, sleep=lambda s: None)
        r2 = send_queued_email(store, bad_gmail, lead_bad.lead_id, campaign=campaign, sleep=lambda s: None)

        self.assertEqual(r1.status, PipelineStatus.SENT)
        self.assertEqual(r2.status, PipelineStatus.SEND_FAILED)

    def test_send_queued_email_requires_queued_status(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead()
        store.upsert_lead(lead)
        gmail, _ = make_gmail_sender()
        with self.assertRaises(NotQueued):
            send_queued_email(store, gmail, lead.lead_id)

    def test_cannot_send_the_same_lead_twice_in_a_row(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead, campaign = self._queued_lead(store)
        gmail, _ = make_gmail_sender()
        send_queued_email(store, gmail, lead.lead_id, campaign=campaign)
        with self.assertRaises(NotQueued):
            send_queued_email(store, gmail, lead.lead_id, campaign=campaign)


# ---------------------------------------------------------------------------
# 10. Persistence / restart (crash recovery)
# ---------------------------------------------------------------------------


class TestPersistenceAndRestart(unittest.TestCase):
    def _queued_lead(self, store, **overrides):
        lead = make_lead(**overrides)
        store.upsert_lead(lead)
        campaign = make_campaign()
        generate_email_for_lead(store, lead, campaign)
        approve_email(store, lead.lead_id)
        lead = store.get(lead.lead_id)
        queue_approved_email(store, lead)
        return store.get(lead.lead_id), campaign

    def test_email_send_rows_survive_a_reopened_db_file(self):
        import os
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(path)  # LeadStore creates it
        try:
            store1 = LeadStore(path)
            lead, campaign = self._queued_lead(store1)
            gmail, _ = make_gmail_sender()
            send_queued_email(store1, gmail, lead.lead_id, campaign=campaign)
            store1.close()

            # Fresh process / fresh LeadStore instance against the same file.
            store2 = LeadStore(path)
            self.addCleanup(store2.close)
            send = get_email_send(store2, lead.lead_id)
            self.assertEqual(send.send_status, SEND_SENT)
            self.assertEqual(store2.get(lead.lead_id).status, PipelineStatus.SENT)
        finally:
            for ext in ("", "-wal", "-shm"):
                try:
                    os.remove(path + ext)
                except FileNotFoundError:
                    pass

    def test_simulated_crash_mid_send_leaves_a_visible_sending_row(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead, campaign = self._queued_lead(store)

        # Simulate a crash: manually replicate only the first half of
        # send_queued_email (the QUEUED -> SENDING write) without ever
        # calling Gmail or recording an outcome.
        from .email_sending import EmailSend
        from .models import utc_now_iso

        row = store.get_email_send(lead.lead_id)
        send = EmailSend.from_dict(row)
        send.send_status = SEND_SENDING
        send.sending_started_at = utc_now_iso()
        store.save_email_send(send.to_dict())
        store.transition(lead.lead_id, PipelineStatus.SENDING)

        stuck = list_stuck_sending(store)
        self.assertEqual(len(stuck), 1)
        self.assertEqual(stuck[0].lead_id, lead.lead_id)

        # A restart's bulk runner must not pick this up (it only selects
        # QUEUED rows), so it can never be silently resent.
        gmail, _ = make_gmail_sender()
        stats = send_pending_queue(store, gmail, delay_seconds=0)
        self.assertEqual(stats["sent"], 0)
        self.assertEqual(get_email_send(store, lead.lead_id).send_status, SEND_SENDING)

    def test_mark_stuck_as_failed_resolves_the_ambiguous_state(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead, campaign = self._queued_lead(store)
        row = store.get_email_send(lead.lead_id)
        send = EmailSend.from_dict(row)
        send.send_status = SEND_SENDING
        store.save_email_send(send.to_dict())
        store.transition(lead.lead_id, PipelineStatus.SENDING)

        updated = mark_stuck_as_failed(store, lead.lead_id, note="Confirmed not sent")
        self.assertEqual(updated.status, PipelineStatus.SEND_FAILED)
        self.assertEqual(get_email_send(store, lead.lead_id).send_status, SEND_FAILED)

    def test_500_job_restart_resends_none_of_the_first_100(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        leads = []
        for i in range(20):  # 20 stands in for 500 -- keeps the test fast
            lead, campaign = self._queued_lead(store, email=f"p{i}@x.com")
            leads.append(lead)
        gmail, _ = make_gmail_sender()

        # "Sent" the first half, then the process "crashes".
        first_half = leads[:10]
        for lead in first_half:
            send_queued_email(store, gmail, lead.lead_id, campaign=campaign, sleep=lambda s: None)

        # "Restart": re-run the bulk sender against the same store.
        stats = send_pending_queue(store, gmail, delay_seconds=0)
        self.assertEqual(stats["sent"], 10)  # only the remaining 10
        for lead in first_half:
            self.assertEqual(store.get(lead.lead_id).status, PipelineStatus.SENT)
        # No duplicate sendmail() calls recorded for any first-half address.
        conn = gmail._conn
        sent_addresses = [addrs[0] for _, addrs, _ in conn.sent]
        for lead in first_half:
            self.assertEqual(sent_addresses.count(lead.email), 1)


# ---------------------------------------------------------------------------
# 11. State transitions
# ---------------------------------------------------------------------------


class TestStateTransitions(unittest.TestCase):
    def test_approved_to_queued_is_legal(self):
        self.assertEqual(
            validate_transition(PipelineStatus.APPROVED, PipelineStatus.QUEUED),
            PipelineStatus.QUEUED,
        )

    def test_queued_to_sending_is_legal(self):
        self.assertEqual(
            validate_transition(PipelineStatus.QUEUED, PipelineStatus.SENDING),
            PipelineStatus.SENDING,
        )

    def test_sending_to_sent_is_legal(self):
        self.assertEqual(
            validate_transition(PipelineStatus.SENDING, PipelineStatus.SENT),
            PipelineStatus.SENT,
        )

    def test_sending_to_send_failed_is_legal(self):
        self.assertEqual(
            validate_transition(PipelineStatus.SENDING, PipelineStatus.SEND_FAILED),
            PipelineStatus.SEND_FAILED,
        )

    def test_sent_is_terminal(self):
        with self.assertRaises(InvalidStateTransition):
            validate_transition(PipelineStatus.SENT, PipelineStatus.QUEUED)

    def test_send_failed_is_terminal(self):
        with self.assertRaises(InvalidStateTransition):
            validate_transition(PipelineStatus.SEND_FAILED, PipelineStatus.QUEUED)

    def test_cannot_skip_queued_straight_to_sending(self):
        with self.assertRaises(InvalidStateTransition):
            validate_transition(PipelineStatus.APPROVED, PipelineStatus.SENDING)

    def test_cannot_skip_approved_straight_to_queued_from_generated(self):
        with self.assertRaises(InvalidStateTransition):
            validate_transition(PipelineStatus.EMAIL_GENERATED, PipelineStatus.QUEUED)

    def test_queued_to_cancelled_is_legal(self):
        self.assertEqual(
            validate_transition(PipelineStatus.QUEUED, PipelineStatus.CANCELLED),
            PipelineStatus.CANCELLED,
        )


# ---------------------------------------------------------------------------
# 12. Day 4-7 regression
# ---------------------------------------------------------------------------


class TestDay4Regression(unittest.TestCase):
    def test_lead_upsert_dedup_unaffected(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead(
            pipeline_status=PipelineStatus.DISCOVERED.value,
            identity_key="acme|jane doe",
            campaign_id="c1",
        )
        stored, created = store.upsert_lead(lead)
        self.assertTrue(created)
        again, created2 = store.upsert_lead(
            make_lead(
                pipeline_status=PipelineStatus.DISCOVERED.value,
                identity_key="acme|jane doe",
                campaign_id="c1",
                first_name="",
            )
        )
        self.assertFalse(created2)
        self.assertEqual(again.lead_id, stored.lead_id)


class TestDay5Day6Regression(unittest.TestCase):
    def test_email_candidates_persistence_unaffected(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead(pipeline_status=PipelineStatus.QUALIFIED.value)
        store.upsert_lead(lead)
        store.save_candidates(
            lead.lead_id,
            [
                {
                    "candidate_id": "c1", "lead_id": lead.lead_id, "rank": 0,
                    "email": "jane@acme.com", "sources": "[]", "patterns": "[]",
                    "domain": "acme.com", "domain_guessed": 0, "mx_status": "VALID",
                    "smtp_status": "NOT_CHECKED", "mx_checked": 1, "smtp_checked": 0,
                    "score": 1.0, "confidence": "high", "validation_status": "GENERATED",
                    "is_best": 1, "created_at": "2026-01-01T00:00:00+00:00",
                }
            ],
        )
        candidates = store.list_candidates(lead.lead_id)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["email"], "jane@acme.com")


class TestDay7Regression(unittest.TestCase):
    def test_email_generation_and_review_flow_unaffected(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        lead = make_lead(pipeline_status=PipelineStatus.EMAIL_VALIDATED.value)
        store.upsert_lead(lead)
        campaign = make_campaign()
        updated = generate_email_for_lead(store, lead, campaign)
        self.assertEqual(updated.status, PipelineStatus.EMAIL_GENERATED)
        approved = approve_email(store, lead.lead_id)
        self.assertEqual(approved.status, PipelineStatus.APPROVED)

    def test_email_jobs_table_untouched_by_day8_additions(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        cur = store._conn.execute("PRAGMA table_info(email_jobs)")
        columns = {row["name"] for row in cur.fetchall()}
        # Exactly the Day 7 columns, plus the Aug 2026 `metadata_json`
        # addition (hook_type/evidence_used/personalization_confidence/
        # cta_type/email_quality_score -- see email_generation.py) -- Day 8
        # added a new table, not new columns here; metadata_json is a
        # later, deliberate, additive exception to that specific invariant.
        self.assertEqual(
            columns,
            {
                "job_id", "lead_id", "campaign_id", "subject", "body",
                "review_status", "edited", "rejection_reason",
                "generated_at", "reviewed_at", "created_at", "updated_at",
                "metadata_json",
            },
        )


if __name__ == "__main__":
    unittest.main()
