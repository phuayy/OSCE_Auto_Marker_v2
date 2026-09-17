from __future__ import annotations

from typing import TYPE_CHECKING

from app.mail.base import EmailSender
from app.mail.console import ConsoleEmailSender
from app.mail.smtp import SmtpConfig, SmtpEmailSender

if TYPE_CHECKING:  # pragma: no cover
    from app.core.config import Settings


def create_email_sender(settings: "Settings") -> EmailSender:
    """The one backend this deployment delivers through, from ``EMAIL_BACKEND``.

    ``smtp`` with no host refuses to build rather than failing on the first
    invitation, for the same reason the GCS factory refuses without a bucket:
    a misconfiguration should stop the boot, not a colleague's onboarding.
    """
    backend = str(settings.email_backend or "console").strip().lower()
    if backend == "console":
        return ConsoleEmailSender()
    if backend == "smtp":
        return SmtpEmailSender(
            SmtpConfig(
                host=settings.smtp_host,
                port=settings.smtp_port,
                username=settings.smtp_username,
                password=settings.smtp_password,
                starttls=settings.smtp_starttls,
                use_tls=settings.smtp_use_tls,
                timeout_seconds=settings.smtp_timeout_seconds,
                sender=settings.email_from,
            )
        )
    raise ValueError(f"Unknown EMAIL_BACKEND '{backend}'. Expected 'console' or 'smtp'.")
