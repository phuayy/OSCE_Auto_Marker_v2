from __future__ import annotations

import logging
from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage

from app.mail.base import EmailDeliveryError, EmailMessage


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SmtpConfig:
    host: str
    port: int = 587
    username: str = ""
    password: str = ""
    # STARTTLS on a plaintext port (587) is what institutional relays and the
    # big providers expect; implicit TLS (465) is the other common shape.
    starttls: bool = True
    use_tls: bool = False
    timeout_seconds: float = 15.0
    sender: str = ""


class SmtpEmailSender:
    """Delivers through an SMTP relay with ``aiosmtplib``.

    Async end to end, so a slow relay stalls the one request that is sending
    rather than the event loop. Every failure — connection refused, bad
    credentials, a rejected recipient — is reported as
    :class:`EmailDeliveryError` with the relay's own wording, because the
    person who can fix it is reading the admin screen, not the log.
    """

    backend = "smtp"
    configured = True

    def __init__(self, config: SmtpConfig) -> None:
        if not config.host.strip():
            raise ValueError("SMTP_HOST is required when EMAIL_BACKEND=smtp.")
        if not config.sender.strip():
            raise ValueError("EMAIL_FROM is required when EMAIL_BACKEND=smtp.")
        self.config = config

    async def send(self, message: EmailMessage) -> None:
        try:
            import aiosmtplib
        except ImportError as error:  # pragma: no cover - dependency is pinned
            raise EmailDeliveryError(
                "The aiosmtplib package is not installed; run 'uv sync' to add it."
            ) from error

        mime = MimeMessage()
        mime["From"] = self.config.sender
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.text)
        if message.html:
            mime.add_alternative(message.html, subtype="html")

        try:
            await aiosmtplib.send(
                mime,
                hostname=self.config.host,
                port=self.config.port,
                username=self.config.username or None,
                password=self.config.password or None,
                start_tls=self.config.starttls if not self.config.use_tls else False,
                use_tls=self.config.use_tls,
                timeout=self.config.timeout_seconds,
            )
        except (aiosmtplib.SMTPConnectError, aiosmtplib.SMTPTimeoutError, OSError) as error:
            # Never got a conversation going: the relay is down, unreachable,
            # or the host/port is wrong. Named so the admin can check the
            # setting rather than the message.
            logger.warning("SMTP connection to %s:%s failed: %s", self.config.host, self.config.port, error)
            raise EmailDeliveryError(
                f"Could not reach the mail server at {self.config.host}:{self.config.port}: {error}"
            ) from error
        except aiosmtplib.SMTPException as error:
            # The relay answered and said no: bad credentials, a rejected
            # sender or recipient, a policy refusal.
            logger.warning("SMTP delivery to %s failed: %s", message.to, error)
            raise EmailDeliveryError(f"The mail server refused the message: {error}") from error
