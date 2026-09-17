"""The mail package: message rendering, link shape, backend selection."""

from __future__ import annotations

import asyncio
import logging

import pytest

from app.core.config import Settings
from app.mail import create_email_sender, templates
from app.mail.base import EmailDeliveryError, EmailMessage, EmailSender
from app.mail.console import ConsoleEmailSender
from app.mail.links import invite_link, password_reset_link
from app.mail.smtp import SmtpConfig, SmtpEmailSender


def _settings(**overrides) -> Settings:
    base = {"ffmpeg_bin": "ffmpeg", "ffprobe_bin": "ffprobe", "scorer_python_bin": "python"}
    return Settings(**{**base, **overrides})


# --- links -----------------------------------------------------------------------


def test_links_put_the_token_in_the_fragment_and_tolerate_a_trailing_slash() -> None:
    assert invite_link("https://osce.example.edu/", "abc") == "https://osce.example.edu/#/accept-invite/abc"
    assert password_reset_link("https://osce.example.edu", "abc") == "https://osce.example.edu/#/reset-password/abc"
    # A token is URL-safe base64, but the encoder is there for anything else.
    assert invite_link("http://localhost:5173", "a/b c") == "http://localhost:5173/#/accept-invite/a%2Fb%20c"


# --- templates ------------------------------------------------------------------


def test_invitation_carries_the_link_in_text_and_html_and_escapes_names() -> None:
    message = templates.invitation(
        to="m@example.edu",
        display_name="<b>Marker</b>",
        invited_by="Admin & Co",
        link="https://x/#/accept-invite/t",
        expires_hours=72,
    )
    assert message.to == "m@example.edu"
    assert "https://x/#/accept-invite/t" in message.text
    assert "https://x/#/accept-invite/t" in message.html
    assert "72 hours" in message.text
    assert "<b>Marker</b>" in message.text  # plain text is not markup
    assert "&lt;b&gt;Marker&lt;/b&gt;" in message.html
    assert "Admin &amp; Co" in message.html
    assert "<b>Marker</b>" not in message.html


def test_invitation_without_names_still_reads() -> None:
    message = templates.invitation(to="m@example.edu", display_name="", invited_by="", link="L", expires_hours=1)
    assert message.text.startswith("Hello,")
    assert "invited to mark" in message.text


def test_reset_and_changed_messages() -> None:
    reset = templates.password_reset(
        to="m@example.edu", display_name="M", link="https://x/#/reset-password/t", expires_minutes=30
    )
    assert "30 minutes" in reset.text and "https://x/#/reset-password/t" in reset.text
    changed = templates.password_changed(to="m@example.edu", display_name="M")
    assert "was changed" in changed.subject
    assert changed.html and "disable the account" in changed.text


# --- backends ---------------------------------------------------------------------


def test_console_backend_logs_instead_of_sending(caplog) -> None:
    sender = ConsoleEmailSender()
    assert isinstance(sender, EmailSender)
    assert sender.configured is False
    with caplog.at_level(logging.INFO, logger="app.mail.console"):
        asyncio.run(sender.send(EmailMessage(to="m@example.edu", subject="Hi", text="line one\nhttps://x/#/y")))
    assert "m@example.edu" in caplog.text
    assert "https://x/#/y" in caplog.text


def test_factory_picks_the_backend_and_refuses_a_half_configured_smtp() -> None:
    assert isinstance(create_email_sender(_settings(email_backend="console")), ConsoleEmailSender)
    smtp = create_email_sender(
        _settings(email_backend="smtp", smtp_host="relay.example.edu", email_from="no-reply@example.edu")
    )
    assert isinstance(smtp, SmtpEmailSender) and smtp.configured is True
    with pytest.raises(ValueError, match="SMTP_HOST"):
        create_email_sender(_settings(email_backend="smtp", email_from="no-reply@example.edu"))
    with pytest.raises(ValueError, match="EMAIL_FROM"):
        create_email_sender(_settings(email_backend="smtp", smtp_host="relay"))
    with pytest.raises(ValueError, match="Unknown EMAIL_BACKEND"):
        create_email_sender(_settings(email_backend="carrier-pigeon"))


def test_smtp_backend_reports_an_unreachable_relay_as_a_delivery_error() -> None:
    # Port 9 (discard) on localhost is closed on any ordinary machine, so the
    # connection is refused at once; the point is the error type and wording.
    sender = SmtpEmailSender(SmtpConfig(host="127.0.0.1", port=9, sender="x@example.edu", timeout_seconds=1.0))
    with pytest.raises(EmailDeliveryError) as excinfo:
        asyncio.run(sender.send(EmailMessage(to="m@example.edu", subject="s", text="t")))
    assert excinfo.value.status_code == 502
    assert "127.0.0.1:9" in excinfo.value.message
