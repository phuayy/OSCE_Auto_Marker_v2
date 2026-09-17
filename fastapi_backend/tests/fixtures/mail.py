"""One mail double for the whole suite.

Records every message instead of sending it, and hands back the link a
message carries so a test can follow an invitation or a reset the way a
person would — by opening what the email said. ``configured`` is a knob:
True imitates a deployment with an SMTP relay (the admin screen must then
*not* be offered the link), False imitates the console backend.
"""

from __future__ import annotations

import re

from app.mail.base import EmailDeliveryError, EmailMessage


_LINK = re.compile(r"https?://\S+#/(?:accept-invite|reset-password)/\S+")


class RecordingEmailSender:
    backend = "recording"

    def __init__(self, *, configured: bool = True, fail_with: str = "") -> None:
        self.configured = configured
        self.sent: list[EmailMessage] = []
        # Set to a message to make every send raise, imitating a dead relay.
        self.fail_with = fail_with

    async def send(self, message: EmailMessage) -> None:
        if self.fail_with:
            raise EmailDeliveryError(self.fail_with)
        self.sent.append(message)

    @property
    def last(self) -> EmailMessage | None:
        return self.sent[-1] if self.sent else None

    def last_link(self) -> str:
        """The action link in the most recent message, or an empty string."""
        if self.last is None:
            return ""
        match = _LINK.search(self.last.text)
        return match.group(0) if match else ""

    def last_token(self) -> str:
        """The raw token in the most recent message's link (after the last slash)."""
        link = self.last_link()
        return link.rsplit("/", 1)[-1] if link else ""

    def messages_to(self, address: str) -> list[EmailMessage]:
        return [message for message in self.sent if message.to == address]
