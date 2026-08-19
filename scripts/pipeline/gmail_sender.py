"""Day 8: Gmail sending adapter.

This wraps the user-supplied `Automatic-Email-Sender` script
(https://github.com/<owner>/Automatic-Email-Sender, EmailSender.py) into a
reusable, testable, credential-safe adapter.

What was reused from EmailSender.py, as-is:
  - The core transport: `smtplib.SMTP_SSL("smtp.gmail.com", 465)` +
    `.login(address, password)` + `.sendmail(...)`. That is a normal,
    still-working way to send through a Gmail account via an "App
    Password", and is exactly the mechanism the original script used.
  - The MIME construction pattern: `MIMEMultipart` + `MIMEText` for the
    body, `MIMEBase` + `email.encoders.encode_base64` for attachments.
    Kept because it's correct, standard-library, and is what the original
    script did to build a message.

What was deliberately NOT reused, and why:
  - Hard-coded credentials. The original had two module-level string
    constants holding a literal placeholder email address and a literal
    placeholder password, meant to be edited directly in the source file.
    Day 8 requires credentials never be hard-coded, so
    this adapter only ever reads them from environment variables
    (GMAIL_ADDRESS / GMAIL_APP_PASSWORD) or explicit constructor args
    (used by tests/mocks) -- never from a literal in source.
  - Login with a full Google account password. The original's comment
    ("give access to less secure apps") describes a login mode Google
    removed in 2022; a normal account password no longer authenticates
    over SMTP. The only currently-working equivalent is a 16-character
    Gmail "App Password" (Google Account -> Security -> 2-Step
    Verification -> App passwords), which is what GMAIL_APP_PASSWORD is
    documented to expect.
  - Hard-coded local filenames (`sahil.csv`, `cfp_email-body1.html`,
    `attach_1.pdf`, ...) and the CSV/template-file reading functions built
    around them. Not applicable here: recipients and rendered subject/body
    come from the SQLite-persisted EmailJob (Day 7's email_generation.py),
    not from local files. `get_contacts`/`read_template` are not carried
    over.
  - Crash-on-first-failure behavior. The original's `main()` loop had no
    try/except at all -- one bad address or a missing attachment file
    would raise and abort every remaining send with no record of what had
    already gone out. `send()` here never raises for send-time failures;
    it always returns a `SendResult`, so a caller (email_sending.py) can
    record a per-lead failure and continue the batch, and so retry logic
    can be layered on top explicitly rather than accidentally.

Nothing about Gmail's own sending limits or anti-abuse systems is worked
around here. This module makes exactly one exposed choice about pacing
(none -- it sends whatever single message it's asked to, once, per call)
and leaves batch pacing/caps entirely to the caller (see
email_sending.py's conservative defaults).
"""

from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from email import encoders
from email import utils as email_utils
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Callable

DEFAULT_SMTP_HOST = "smtp.gmail.com"
DEFAULT_SMTP_PORT = 465


class GmailCredentialsError(RuntimeError):
    """Raised when GMAIL_ADDRESS / GMAIL_APP_PASSWORD aren't available."""


@dataclass
class SendResult:
    """Outcome of one send attempt.

    `message_id` is the RFC 5322 Message-ID header this adapter generated
    and put on the outgoing message *before* sending it -- plain SMTP
    (unlike the Gmail API) doesn't hand back a provider-confirmed ID in
    its response, so this self-generated, globally-unique ID is the
    closest thing available in SMTP mode, and is what's recorded as
    `provider_message_id` downstream. It's still useful for tracing a
    specific send (it's the same value Gmail stores in the sent message's
    own Message-ID header), just not a delivery receipt.
    """

    success: bool
    message_id: str = ""
    error: str = ""


class GmailSender:
    """Thin, mockable adapter around Gmail's SMTP endpoint.

    Credentials are only ever read from environment variables (or passed
    explicitly, which is how tests inject fake ones) -- never hard-coded.
    In real use, call `pipeline.config.load_env()` before constructing this
    (exactly like every other pipeline stage's `__main__` block does) so
    `.env` / `~/.hermes/.env` are picked up.

    `smtp_client_factory` is injectable so tests can swap in a fake SMTP
    client and never touch the network or send a real email.
    """

    def __init__(
        self,
        address: str | None = None,
        app_password: str | None = None,
        *,
        smtp_host: str = DEFAULT_SMTP_HOST,
        smtp_port: int = DEFAULT_SMTP_PORT,
        smtp_client_factory: Callable[[str, int], object] = smtplib.SMTP_SSL,
    ) -> None:
        self.address = address if address is not None else os.environ.get("GMAIL_ADDRESS", "")
        self.app_password = (
            app_password if app_password is not None else os.environ.get("GMAIL_APP_PASSWORD", "")
        )
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self._smtp_client_factory = smtp_client_factory
        self._conn = None

    # -- credential / config validation --------------------------------------

    def validate_credentials(self) -> None:
        """Raise GmailCredentialsError if address/app_password aren't set.

        Pure config check -- no network call, so this can (and should) run
        before anything ever tries to talk to Gmail.
        """
        missing = []
        if not self.address:
            missing.append("GMAIL_ADDRESS")
        if not self.app_password:
            missing.append("GMAIL_APP_PASSWORD")
        if missing:
            raise GmailCredentialsError(
                "Missing Gmail credentials: "
                + ", ".join(missing)
                + ". Set these as environment variables (e.g. in a .env file) -- "
                "GMAIL_APP_PASSWORD must be a 16-character Gmail App Password "
                "(Google Account -> Security -> 2-Step Verification -> App "
                "passwords), not your regular account password."
            )

    # -- connection lifecycle -------------------------------------------------

    def connect(self) -> None:
        """Open and authenticate the SMTP connection. Idempotent."""
        self.validate_credentials()
        if self._conn is not None:
            return
        conn = self._smtp_client_factory(self.smtp_host, self.smtp_port)
        conn.login(self.address, self.app_password)
        self._conn = conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.quit()
            except Exception:
                pass
            self._conn = None

    def __enter__(self) -> "GmailSender":
        self.connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def verify_connection(self) -> bool:
        """Log in and immediately disconnect, without sending anything.

        Confirms the credentials actually authenticate against Gmail. Raises
        GmailCredentialsError (missing config) or smtplib.SMTPException /
        OSError (bad credentials, network issue) on failure.
        """
        self.connect()
        self.close()
        return True

    # -- message construction -------------------------------------------------

    def _build_message(
        self,
        *,
        to_address: str,
        subject: str,
        body_html: str,
        body_text: str | None,
        from_name: str,
        attachments: list[str] | None,
    ):
        msg = MIMEMultipart("mixed")
        msg["From"] = f"{from_name} <{self.address}>" if from_name else self.address
        msg["To"] = to_address
        msg["Subject"] = subject
        message_id = email_utils.make_msgid(domain="gmail.com")
        msg["Message-ID"] = message_id
        msg["Date"] = email_utils.formatdate(localtime=True)

        alt = MIMEMultipart("alternative")
        if body_text:
            alt.attach(MIMEText(body_text, "plain"))
        alt.attach(MIMEText(body_html, "html"))
        msg.attach(alt)

        # Optional attachment support, carried over from the original
        # script's pattern (MIMEBase + base64 encoding) -- off by default,
        # nothing in the Day 8 pipeline currently passes attachments.
        for path in attachments or []:
            p = Path(path)
            with open(p, "rb") as fh:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(fh.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename={p.name}")
            msg.attach(part)

        return msg, message_id

    # -- sending ---------------------------------------------------------------

    def send(
        self,
        to_address: str,
        subject: str,
        body_html: str,
        *,
        body_text: str | None = None,
        from_name: str = "",
        attachments: list[str] | None = None,
    ) -> SendResult:
        """Send one email. Never raises for send-time failures (missing
        credentials, auth errors, refused recipients, network errors, ...)
        -- always returns a SendResult, so a caller sending a batch can
        record a per-recipient failure and keep going instead of aborting
        the whole run, unlike the original script's unguarded loop.
        """
        if not to_address or not to_address.strip():
            return SendResult(success=False, error="Empty recipient address")
        if not subject.strip() or not body_html.strip():
            return SendResult(success=False, error="Empty subject or body")
        try:
            self.connect()
            msg, message_id = self._build_message(
                to_address=to_address,
                subject=subject,
                body_html=body_html,
                body_text=body_text,
                from_name=from_name,
                attachments=attachments,
            )
            self._conn.sendmail(self.address, [to_address], msg.as_string())
            return SendResult(success=True, message_id=message_id)
        except GmailCredentialsError as exc:
            return SendResult(success=False, error=str(exc))
        except smtplib.SMTPException as exc:
            return SendResult(success=False, error=f"{type(exc).__name__}: {exc}")
        except OSError as exc:
            return SendResult(success=False, error=f"Connection error: {exc}")

    def send_test_email(self, to_address: str | None = None) -> SendResult:
        """Item 9: a real test send to verify Gmail authentication before
        running a campaign. Defaults to sending to the configured Gmail
        address itself, so you can verify without emailing a third party.
        """
        target = to_address or self.address
        return self.send(
            target,
            subject="Prospect Automation - Gmail test email",
            body_html=(
                "<p>This is a test email from the prospect-automation "
                "pipeline's GmailSender adapter.</p>"
                "<p>If you received this, Gmail sending is configured "
                "correctly and the pipeline is ready to send a real "
                "campaign.</p>"
            ),
            body_text=(
                "This is a test email from the prospect-automation "
                "pipeline's GmailSender adapter. If you received this, "
                "Gmail sending is configured correctly and the pipeline "
                "is ready to send a real campaign."
            ),
        )
