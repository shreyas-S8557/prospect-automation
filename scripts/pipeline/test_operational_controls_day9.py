"""Day 9 milestone tests: operational reliability + campaign controls
layered on top of Day 8's sending queue.

Run with (from the `scripts/` directory, or as a module from repo root):
    python -m unittest pipeline.test_operational_controls_day9 -v

No network and no real Gmail account is ever touched, same convention as
Day 8: GmailSender is always constructed with a fake `smtp_client_factory`.
Every store is an in-memory SQLite DB.

Covers (per the Day 9 spec):
  1. Campaign-level pause/resume
  2. Explicit campaign stop/cancellation
  3. Configurable per-run sending limit
  4. Configurable inter-send delay
  5. Retry controls
  6. Duplicate-send protection
  7. Suppression / do-not-contact list
  8. Campaign progress/statistics
  9. Handling of leads stuck in SENDING after a crash (conservative,
     manual resolution -- never auto-resent)
  10. Logging/error reporting (exceptions carry clear, specific messages)
  + Day 4-8 regression smoke
"""

from __future__ import annotations

import unittest

from .campaign import create_campaign
from .campaign_control import (
    CampaignAlreadyStopped,
    CampaignControl,
    RUN_STATE_PAUSED,
    RUN_STATE_RUNNING,
    RUN_STATE_STOPPED,
    configure_sending,
    get_campaign_control,
    get_run_state,
    pause_campaign,
    resume_campaign,
    stop_campaign,
)
from .campaign_stats import get_campaign_stats
from .email_generation import approve_email, generate_email_for_lead
from .email_sending import (
    DEFAULT_MAX_RETRIES,
    DuplicateSendBlocked,
    NoApprovedEmailJob,
    NotQueued,
    SEND_CANCELLED,
    SEND_FAILED,
    SEND_QUEUED,
    SEND_SENDING,
    SEND_SENT,
    SuppressedRecipient,
    get_email_send,
    list_email_sends,
    list_stuck_sending,
    mark_stuck_as_failed,
    queue_approved_email,
    queue_pending_approvals,
    resolve_stuck_as_sent,
    send_pending_queue,
    send_queued_email,
)
from .gmail_sender import GmailSender
from .lead_store import LeadStore
from .models import InvalidStateTransition, Lead, PipelineStatus
from .suppression import (
    already_contacted,
    is_suppressed,
    list_suppressed,
    suppress_email,
    unsuppress_email,
)


# ---------------------------------------------------------------------------
# Shared fakes / fixtures (mirrors test_email_sending_day8.py)
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


def make_gmail_sender(**kwargs) -> GmailSender:
    login_exc = kwargs.pop("login_exc", None)
    send_exc_sequence = kwargs.pop("send_exc_sequence", None)

    def factory(host, port):
        return FakeSMTPClient(host, port, login_exc=login_exc, send_exc_sequence=send_exc_sequence)

    return GmailSender(
        address=kwargs.pop("address", "me@gmail.com"),
        app_password=kwargs.pop("app_password", "abcd efgh ijkl mnop"),
        smtp_client_factory=factory,
        **kwargs,
    )


def approved_lead(store, campaign, **overrides):
    """DISCOVERED-free shortcut: build a lead straight through to APPROVED
    against `campaign`, matching Day 8's test fixtures."""
    lead = make_lead(**overrides)
    lead.campaign_id = campaign.campaign_id
    store.upsert_lead(lead)
    generate_email_for_lead(store, lead, campaign)
    approve_email(store, lead.lead_id)
    return store.get(lead.lead_id)


def queued_lead(store, campaign, **overrides):
    lead = approved_lead(store, campaign, **overrides)
    queue_approved_email(store, lead)
    return store.get(lead.lead_id)


# ---------------------------------------------------------------------------
# 1. Pause / resume
# ---------------------------------------------------------------------------


class TestPauseResume(unittest.TestCase):
    def test_new_campaign_defaults_to_running(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        self.assertEqual(get_run_state(store, "camp-1"), RUN_STATE_RUNNING)

    def test_pause_sets_run_state(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        control = pause_campaign(store, "camp-1")
        self.assertEqual(control.run_state, RUN_STATE_PAUSED)
        self.assertEqual(get_run_state(store, "camp-1"), RUN_STATE_PAUSED)
        self.assertTrue(control.paused_at)

    def test_resume_sets_run_state_back_to_running(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        pause_campaign(store, "camp-1")
        control = resume_campaign(store, "camp-1")
        self.assertEqual(control.run_state, RUN_STATE_RUNNING)
        self.assertEqual(get_run_state(store, "camp-1"), RUN_STATE_RUNNING)

    def test_pause_is_idempotent(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        pause_campaign(store, "camp-1")
        pause_campaign(store, "camp-1")  # should not raise
        self.assertEqual(get_run_state(store, "camp-1"), RUN_STATE_PAUSED)

    def test_paused_campaign_halts_queueing_mid_run(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        campaign = make_campaign()
        from .campaign import save_campaign

        save_campaign(store, campaign)
        approved_lead(store, campaign, email="a@x.com")
        approved_lead(store, campaign, email="b@x.com")

        pause_campaign(store, campaign.campaign_id)
        stats = queue_pending_approvals(store, campaign_id=campaign.campaign_id)
        self.assertEqual(stats["queued"], 0)

    def test_paused_campaign_halts_sending_mid_run(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        queued_lead(store, campaign, email="a@x.com")
        queued_lead(store, campaign, email="b@x.com")
        gmail = make_gmail_sender()

        pause_campaign(store, campaign.campaign_id)
        stats = send_pending_queue(store, gmail, campaign=campaign, delay_seconds=0)
        self.assertEqual(stats["sent"], 0)
        self.assertTrue(stats["halted"])
        # Both leads remain QUEUED, untouched -- ready for a later resume.
        remaining = list_email_sends(store, campaign_id=campaign.campaign_id, send_status=SEND_QUEUED)
        self.assertEqual(len(remaining), 2)

    def test_resume_allows_sending_to_continue(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        queued_lead(store, campaign, email="a@x.com")
        gmail = make_gmail_sender()

        pause_campaign(store, campaign.campaign_id)
        send_pending_queue(store, gmail, campaign=campaign, delay_seconds=0)
        resume_campaign(store, campaign.campaign_id)
        stats = send_pending_queue(store, gmail, campaign=campaign, delay_seconds=0)
        self.assertEqual(stats["sent"], 1)

    def test_cannot_pause_a_stopped_campaign(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        stop_campaign(store, "camp-1")
        with self.assertRaises(CampaignAlreadyStopped):
            pause_campaign(store, "camp-1")

    def test_cannot_resume_a_stopped_campaign(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        stop_campaign(store, "camp-1")
        with self.assertRaises(CampaignAlreadyStopped):
            resume_campaign(store, "camp-1")


# ---------------------------------------------------------------------------
# 2. Stop / cancellation
# ---------------------------------------------------------------------------


class TestStopCancellation(unittest.TestCase):
    def test_stop_sets_run_state_stopped(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        control = stop_campaign(store, "camp-1")
        self.assertEqual(control.run_state, RUN_STATE_STOPPED)
        self.assertTrue(control.stopped_at)

    def test_stop_is_idempotent(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        stop_campaign(store, "camp-1")
        stop_campaign(store, "camp-1")  # should not raise
        self.assertEqual(get_run_state(store, "camp-1"), RUN_STATE_STOPPED)

    def test_stop_cancels_queued_leads_and_sends(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        lead = queued_lead(store, campaign, email="a@x.com")

        stop_campaign(store, campaign.campaign_id)

        updated_lead = store.get(lead.lead_id)
        self.assertEqual(updated_lead.status, PipelineStatus.CANCELLED)
        send = get_email_send(store, lead.lead_id)
        self.assertEqual(send.send_status, SEND_CANCELLED)

    def test_stop_does_not_touch_already_sent_leads(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        lead = queued_lead(store, campaign, email="a@x.com")
        gmail = make_gmail_sender()
        send_queued_email(store, gmail, lead.lead_id, campaign=campaign)

        stop_campaign(store, campaign.campaign_id)

        updated_lead = store.get(lead.lead_id)
        self.assertEqual(updated_lead.status, PipelineStatus.SENT)
        self.assertEqual(get_email_send(store, lead.lead_id).send_status, SEND_SENT)

    def test_stopped_campaign_blocks_further_queueing_and_sending(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        approved_lead(store, campaign, email="a@x.com")
        stop_campaign(store, campaign.campaign_id)

        stats = queue_pending_approvals(store, campaign_id=campaign.campaign_id)
        self.assertEqual(stats["queued"], 0)

    def test_stop_without_cancel_queued_leaves_queue_intact(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        lead = queued_lead(store, campaign, email="a@x.com")

        stop_campaign(store, campaign.campaign_id, cancel_queued=False)

        self.assertEqual(store.get(lead.lead_id).status, PipelineStatus.QUEUED)
        self.assertEqual(get_email_send(store, lead.lead_id).send_status, SEND_QUEUED)


# ---------------------------------------------------------------------------
# 3 & 4. Configurable per-run limit + inter-send delay
# ---------------------------------------------------------------------------


class TestConfigurableLimitsAndDelay(unittest.TestCase):
    def test_explicit_argument_overrides_everything(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        configure_sending(store, campaign.campaign_id, max_per_run=10)
        for i in range(5):
            queued_lead(store, campaign, email=f"p{i}@x.com")
        gmail = make_gmail_sender()

        stats = send_pending_queue(store, gmail, campaign=campaign, max_per_run=2, delay_seconds=0)
        self.assertEqual(stats["sent"], 2)

    def test_configured_max_per_run_applies_when_not_overridden(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        configure_sending(store, campaign.campaign_id, max_per_run=2)
        for i in range(5):
            queued_lead(store, campaign, email=f"p{i}@x.com")
        gmail = make_gmail_sender()

        stats = send_pending_queue(store, gmail, campaign=campaign, delay_seconds=0)
        self.assertEqual(stats["sent"], 2)

    def test_configured_delay_seconds_applies_when_not_overridden(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        configure_sending(store, campaign.campaign_id, delay_seconds=9.5)
        queued_lead(store, campaign, email="a@x.com")
        queued_lead(store, campaign, email="b@x.com")
        gmail = make_gmail_sender()

        delays = []
        send_pending_queue(store, gmail, campaign=campaign, sleep=delays.append)
        self.assertEqual(delays, [9.5])

    def test_default_used_when_nothing_configured_or_passed(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        control = get_campaign_control(store, "unconfigured-campaign")
        self.assertEqual(control.run_state, RUN_STATE_RUNNING)
        self.assertEqual(control.max_per_run, "")
        self.assertEqual(control.delay_seconds, "")


# ---------------------------------------------------------------------------
# 5. Retry controls
# ---------------------------------------------------------------------------


class TestRetryControls(unittest.TestCase):
    def test_configured_max_retries_applies_to_queueing(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        configure_sending(store, campaign.campaign_id, max_retries=5)
        approved_lead(store, campaign, email="a@x.com")

        queue_pending_approvals(store, campaign_id=campaign.campaign_id)
        sends = list_email_sends(store, campaign_id=campaign.campaign_id)
        self.assertEqual(sends[0].max_retries, 5)

    def test_explicit_max_retries_overrides_configured(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        configure_sending(store, campaign.campaign_id, max_retries=5)
        approved_lead(store, campaign, email="a@x.com")

        queue_pending_approvals(store, campaign_id=campaign.campaign_id, max_retries=1)
        sends = list_email_sends(store, campaign_id=campaign.campaign_id)
        self.assertEqual(sends[0].max_retries, 1)

    def test_configured_retry_backoff_used_between_attempts(self):
        import smtplib

        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        configure_sending(store, campaign.campaign_id, retry_backoff_seconds=7.0)
        lead = queued_lead(store, campaign, email="a@x.com")
        fails = [smtplib.SMTPServerDisconnected("dropped"), None]
        gmail = make_gmail_sender(send_exc_sequence=fails)

        backoffs = []
        send_pending_queue(store, gmail, campaign=campaign, sleep=backoffs.append)
        # First recorded sleep is the retry backoff (7.0); no inter-send
        # delay follows since this is the only queued lead.
        self.assertIn(7.0, backoffs)

    def test_default_max_retries_is_used_with_no_config(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        approved_lead(store, campaign, email="a@x.com")
        queue_pending_approvals(store, campaign_id=campaign.campaign_id)
        sends = list_email_sends(store, campaign_id=campaign.campaign_id)
        self.assertEqual(sends[0].max_retries, DEFAULT_MAX_RETRIES)


# ---------------------------------------------------------------------------
# 6. Duplicate-send protection
# ---------------------------------------------------------------------------


class TestDuplicateSendProtectionDay9(unittest.TestCase):
    def test_second_lead_with_same_email_is_blocked_after_first_is_queued(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        lead1 = approved_lead(store, campaign, email="dup@x.com")
        queue_approved_email(store, lead1)

        # A second, distinct Lead record (different lead_id) that happens
        # to resolve to the same email address (e.g. re-discovered under a
        # different identity_key).
        lead2 = approved_lead(store, campaign, email="dup@x.com", first_name="Janet")
        with self.assertRaises(DuplicateSendBlocked):
            queue_approved_email(store, lead2)

    def test_duplicate_is_blocked_after_the_first_has_actually_sent(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        lead1 = queued_lead(store, campaign, email="dup@x.com")
        gmail = make_gmail_sender()
        send_queued_email(store, gmail, lead1.lead_id, campaign=campaign)

        lead2 = approved_lead(store, campaign, email="dup@x.com", first_name="Janet")
        with self.assertRaises(DuplicateSendBlocked):
            queue_approved_email(store, lead2)

    def test_allow_duplicate_explicitly_overrides_the_block(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        lead1 = approved_lead(store, campaign, email="dup@x.com")
        queue_approved_email(store, lead1)
        lead2 = approved_lead(store, campaign, email="dup@x.com", first_name="Janet")

        updated = queue_approved_email(store, lead2, allow_duplicate=True)
        self.assertEqual(updated.status, PipelineStatus.QUEUED)

    def test_duplicate_check_is_case_insensitive(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        lead1 = approved_lead(store, campaign, email="Dup@X.com")
        queue_approved_email(store, lead1)
        self.assertTrue(already_contacted(store, "dup@x.com", exclude_lead_id="someone-else"))

    def test_queue_pending_approvals_counts_duplicate_block_as_skipped(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        lead1 = approved_lead(store, campaign, email="dup@x.com")
        queue_approved_email(store, lead1)
        approved_lead(store, campaign, email="dup@x.com", first_name="Janet")

        stats = queue_pending_approvals(store, campaign_id=campaign.campaign_id)
        self.assertEqual(stats["queued"], 0)
        self.assertEqual(stats["skipped"], 1)


# ---------------------------------------------------------------------------
# 7. Suppression / do-not-contact list
# ---------------------------------------------------------------------------


class TestSuppressionList(unittest.TestCase):
    def test_suppressed_email_is_reported_as_suppressed(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        suppress_email(store, "blocked@x.com", reason="unsubscribed")
        self.assertTrue(is_suppressed(store, "blocked@x.com"))
        self.assertTrue(is_suppressed(store, "Blocked@X.com "))  # normalized

    def test_unsuppressed_email_is_not_suppressed(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        self.assertFalse(is_suppressed(store, "ok@x.com"))

    def test_queueing_a_suppressed_lead_raises(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        suppress_email(store, "blocked@x.com")
        lead = approved_lead(store, campaign, email="blocked@x.com")

        with self.assertRaises(SuppressedRecipient):
            queue_approved_email(store, lead)

    def test_queue_pending_approvals_skips_suppressed_without_aborting(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        suppress_email(store, "blocked@x.com")
        approved_lead(store, campaign, email="blocked@x.com")
        approved_lead(store, campaign, email="ok@x.com")

        stats = queue_pending_approvals(store, campaign_id=campaign.campaign_id)
        self.assertEqual(stats["queued"], 1)
        self.assertEqual(stats["skipped"], 1)

    def test_unsuppress_allows_queueing_again(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        suppress_email(store, "blocked@x.com")
        lead = approved_lead(store, campaign, email="blocked@x.com")
        unsuppress_email(store, "blocked@x.com")

        updated = queue_approved_email(store, lead)
        self.assertEqual(updated.status, PipelineStatus.QUEUED)

    def test_campaign_scoped_suppression_does_not_block_other_campaigns(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        camp_a = make_campaign(name="A")
        camp_b = make_campaign(name="B")
        save_campaign(store, camp_a)
        save_campaign(store, camp_b)
        suppress_email(store, "scoped@x.com", campaign_id=camp_a.campaign_id)

        self.assertTrue(is_suppressed(store, "scoped@x.com", campaign_id=camp_a.campaign_id))
        self.assertFalse(is_suppressed(store, "scoped@x.com", campaign_id=camp_b.campaign_id))

        lead_b = approved_lead(store, camp_b, email="scoped@x.com")
        updated = queue_approved_email(store, lead_b)
        self.assertEqual(updated.status, PipelineStatus.QUEUED)

    def test_list_suppressed_returns_added_entries(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        suppress_email(store, "a@x.com", reason="bounced")
        suppress_email(store, "b@x.com", reason="unsubscribed")
        entries = list_suppressed(store)
        emails = {e.email_normalized for e in entries}
        self.assertEqual(emails, {"a@x.com", "b@x.com"})

    def test_suppressing_empty_email_raises(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        with self.assertRaises(ValueError):
            suppress_email(store, "")


# ---------------------------------------------------------------------------
# 8. Campaign progress / statistics
# ---------------------------------------------------------------------------


class TestCampaignStats(unittest.TestCase):
    def _campaign(self, store):
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        return campaign

    def test_stats_start_at_zero(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        stats = get_campaign_stats(store, "empty-campaign")
        for key in (
            "discovered", "qualified", "email_found", "validated",
            "generated", "approved", "queued", "sent", "failed", "skipped",
        ):
            self.assertEqual(stats[key], 0)

    def test_discovered_counts_every_lead_regardless_of_stage(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        campaign = self._campaign(store)
        approved_lead(store, campaign, email="a@x.com")
        store.upsert_lead(
            make_lead(email="", campaign_id=campaign.campaign_id, pipeline_status=PipelineStatus.DISCOVERED.value)
        )
        stats = get_campaign_stats(store, campaign.campaign_id)
        self.assertEqual(stats["discovered"], 2)

    def test_funnel_counts_are_cumulative_not_current_bucket(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        campaign = self._campaign(store)
        lead = queued_lead(store, campaign, email="a@x.com")
        gmail = make_gmail_sender()
        send_queued_email(store, gmail, lead.lead_id, campaign=campaign)

        stats = get_campaign_stats(store, campaign.campaign_id)
        # A SENT lead passed through every earlier milestone too.
        for key in ("discovered", "qualified", "email_found", "validated", "generated", "approved", "queued", "sent"):
            self.assertEqual(stats[key], 1, key)

    def test_send_failed_counts_as_failed_not_skipped(self):
        import smtplib

        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        campaign = self._campaign(store)
        lead = queued_lead(store, campaign, email="a@x.com")
        always_fail = [smtplib.SMTPRecipientsRefused({"a@x.com": (550, b"no")})] * 10
        gmail = make_gmail_sender(send_exc_sequence=always_fail)
        send_queued_email(store, gmail, lead.lead_id, campaign=campaign, sleep=lambda s: None)

        stats = get_campaign_stats(store, campaign.campaign_id)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["skipped"], 0)
        # Still counts toward every milestone up to (and including) queued.
        self.assertEqual(stats["queued"], 1)
        self.assertEqual(stats["sent"], 0)

    def test_rejected_lead_counts_as_skipped(self):
        from .email_generation import reject_email

        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        campaign = self._campaign(store)
        lead = make_lead(email="a@x.com", campaign_id=campaign.campaign_id)
        store.upsert_lead(lead)
        generate_email_for_lead(store, lead, campaign)
        reject_email(store, lead.lead_id, reason="not a fit")

        stats = get_campaign_stats(store, campaign.campaign_id)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["generated"], 1)  # reached EMAIL_GENERATED before rejection

    def test_cancelled_from_stop_counts_as_skipped(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        campaign = self._campaign(store)
        queued_lead(store, campaign, email="a@x.com")
        stop_campaign(store, campaign.campaign_id)

        stats = get_campaign_stats(store, campaign.campaign_id)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["queued"], 1)  # reached QUEUED before cancellation

    def test_stats_are_scoped_per_campaign(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        camp_a = self._campaign(store)
        camp_b = make_campaign(name="B")
        from .campaign import save_campaign

        save_campaign(store, camp_b)
        approved_lead(store, camp_a, email="a@x.com")
        approved_lead(store, camp_b, email="b@x.com")
        approved_lead(store, camp_b, email="c@x.com")

        self.assertEqual(get_campaign_stats(store, camp_a.campaign_id)["approved"], 1)
        self.assertEqual(get_campaign_stats(store, camp_b.campaign_id)["approved"], 2)


# ---------------------------------------------------------------------------
# 9. Crash recovery: leads stuck in SENDING
# ---------------------------------------------------------------------------


class TestCrashRecoveryDay9(unittest.TestCase):
    def _stuck_lead(self, store, campaign):
        lead = queued_lead(store, campaign, email="a@x.com")
        # Simulate a crash: mark SENDING (as send_queued_email would,
        # before the network call) and stop there -- no SENT/SEND_FAILED
        # ever gets recorded.
        store.transition(lead.lead_id, PipelineStatus.SENDING)
        send = get_email_send(store, lead.lead_id)
        from .email_sending import EmailSend
        from .models import utc_now_iso

        s = EmailSend.from_dict(send.to_dict())
        s.send_status = SEND_SENDING
        s.sending_started_at = utc_now_iso()
        store.save_email_send(s.to_dict())
        return lead

    def _campaign(self, store):
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        return campaign

    def test_stuck_sending_lead_is_never_auto_requeued_or_resent(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        campaign = self._campaign(store)
        lead = self._stuck_lead(store, campaign)
        gmail = make_gmail_sender()

        # Neither bulk runner should touch it: queueing only pulls
        # APPROVED, sending only pulls QUEUED -- a SENDING row is neither.
        qstats = queue_pending_approvals(store, campaign_id=campaign.campaign_id)
        sstats = send_pending_queue(store, gmail, campaign=campaign, delay_seconds=0)
        self.assertEqual(qstats["queued"], 0)
        self.assertEqual(sstats["sent"], 0)
        self.assertEqual(store.get(lead.lead_id).status, PipelineStatus.SENDING)

    def test_list_stuck_sending_surfaces_it(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        campaign = self._campaign(store)
        lead = self._stuck_lead(store, campaign)

        stuck = list_stuck_sending(store, campaign_id=campaign.campaign_id)
        self.assertEqual([s.lead_id for s in stuck], [lead.lead_id])

    def test_mark_stuck_as_failed_is_manual_and_explicit(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        campaign = self._campaign(store)
        lead = self._stuck_lead(store, campaign)

        updated = mark_stuck_as_failed(store, lead.lead_id, note="confirmed not in Sent folder")
        self.assertEqual(updated.status, PipelineStatus.SEND_FAILED)
        send = get_email_send(store, lead.lead_id)
        self.assertEqual(send.send_status, SEND_FAILED)
        self.assertIn("confirmed not in Sent folder", send.last_error)

    def test_resolve_stuck_as_sent_requires_explicit_confirmation_call(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        campaign = self._campaign(store)
        lead = self._stuck_lead(store, campaign)

        updated = resolve_stuck_as_sent(
            store, lead.lead_id, provider_message_id="<confirmed@gmail.com>", note="found in Sent folder"
        )
        self.assertEqual(updated.status, PipelineStatus.SENT)
        send = get_email_send(store, lead.lead_id)
        self.assertEqual(send.send_status, SEND_SENT)
        self.assertEqual(send.provider_message_id, "<confirmed@gmail.com>")

    def test_cannot_resolve_a_non_stuck_lead(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        campaign = self._campaign(store)
        lead = queued_lead(store, campaign, email="a@x.com")  # still QUEUED, not SENDING

        with self.assertRaises(NotQueued):
            mark_stuck_as_failed(store, lead.lead_id)
        with self.assertRaises(NotQueued):
            resolve_stuck_as_sent(store, lead.lead_id)

    def test_stop_campaign_does_not_touch_stuck_sending_leads(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        campaign = self._campaign(store)
        lead = self._stuck_lead(store, campaign)

        stop_campaign(store, campaign.campaign_id)  # only cancels QUEUED rows
        self.assertEqual(store.get(lead.lead_id).status, PipelineStatus.SENDING)
        self.assertEqual(get_email_send(store, lead.lead_id).send_status, SEND_SENDING)


# ---------------------------------------------------------------------------
# 10. Logging / error reporting
# ---------------------------------------------------------------------------


class TestErrorReporting(unittest.TestCase):
    def test_suppressed_recipient_error_names_the_lead_and_email(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        suppress_email(store, "blocked@x.com")
        lead = approved_lead(store, campaign, email="blocked@x.com")

        with self.assertRaises(SuppressedRecipient) as ctx:
            queue_approved_email(store, lead)
        self.assertIn(lead.lead_id, str(ctx.exception))
        self.assertIn("blocked@x.com", str(ctx.exception))

    def test_duplicate_blocked_error_mentions_override_flag(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        lead1 = approved_lead(store, campaign, email="dup@x.com")
        queue_approved_email(store, lead1)
        lead2 = approved_lead(store, campaign, email="dup@x.com", first_name="Janet")

        with self.assertRaises(DuplicateSendBlocked) as ctx:
            queue_approved_email(store, lead2)
        self.assertIn("allow_duplicate", str(ctx.exception))

    def test_campaign_already_stopped_error_names_the_campaign(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        stop_campaign(store, "camp-xyz")
        with self.assertRaises(CampaignAlreadyStopped) as ctx:
            pause_campaign(store, "camp-xyz")
        self.assertIn("camp-xyz", str(ctx.exception))


# ---------------------------------------------------------------------------
# Day 4-8 regression smoke (Day 9 must not break any prior stage)
# ---------------------------------------------------------------------------


class TestDay4ThroughDay8Regression(unittest.TestCase):
    def test_full_happy_path_discovered_to_sent(self):
        from .campaign import save_campaign
        from .models import validate_transition

        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        campaign = make_campaign()
        save_campaign(store, campaign)

        lead = Lead(
            first_name="Sam",
            last_name="Lee",
            full_name="Sam Lee",
            company_name="Beta LLC",
            email="sam.lee@beta.com",
            campaign_id=campaign.campaign_id,
            pipeline_status=PipelineStatus.DISCOVERED.value,
        )
        stored, created = store.upsert_lead(lead)
        self.assertTrue(created)

        store.transition(stored.lead_id, PipelineStatus.QUALIFIED)
        store.transition(stored.lead_id, PipelineStatus.EMAIL_CANDIDATES_FOUND)
        store.transition(stored.lead_id, PipelineStatus.EMAIL_VALIDATED)
        stored = store.get(stored.lead_id)

        generate_email_for_lead(store, stored, campaign)
        approve_email(store, stored.lead_id)
        stored = store.get(stored.lead_id)

        queue_approved_email(store, stored)
        gmail = make_gmail_sender()
        final = send_pending_queue(store, gmail, campaign=campaign, delay_seconds=0)
        self.assertEqual(final["sent"], 1)
        self.assertEqual(store.get(stored.lead_id).status, PipelineStatus.SENT)

        # Illegal transitions still correctly rejected (Day 4 state machine
        # untouched by Day 9).
        with self.assertRaises(InvalidStateTransition):
            validate_transition(PipelineStatus.SENT, PipelineStatus.QUEUED)

    def test_day8_duplicate_and_persistence_semantics_unchanged(self):
        store = LeadStore(":memory:")
        self.addCleanup(store.close)
        from .campaign import save_campaign

        campaign = make_campaign()
        save_campaign(store, campaign)
        lead = approved_lead(store, campaign, email="a@x.com")
        queue_approved_email(store, lead)
        # Re-running queueing against the same (now-stale, still-
        # APPROVED-in-memory) lead object -- exactly Day 8's crash-restart
        # race -- is still a no-op, not a duplicate.
        result = queue_approved_email(store, lead)
        self.assertEqual(len(store.list_email_sends()), 1)
        self.assertEqual(result.lead_id, lead.lead_id)


if __name__ == "__main__":
    unittest.main()
