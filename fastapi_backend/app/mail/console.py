from __future__ import annotations

import logging

from app.mail.base import EmailMessage


logger = logging.getLogger(__name__)


class ConsoleEmailSender:
    """Writes the message to the server log instead of sending it.

    The development default, and the honest fallback for a deployment with no
    mail relay yet: the invitation link is in the log, and — because
    ``configured`` is False — also offered to the admin to copy. Nothing is
    silently dropped.
    """

    backend = "console"
    configured = False

    async def send(self, message: EmailMessage) -> None:
        logger.info(
            "Email (not sent: EMAIL_BACKEND=console)\n  To: %s\n  Subject: %s\n%s",
            message.to,
            message.subject,
            "\n".join(f"  {line}" for line in message.text.splitlines()),
        )
