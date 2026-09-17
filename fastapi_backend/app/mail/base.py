from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.core.exceptions import AppError


@dataclass(frozen=True)
class EmailMessage:
    """One outbound message. Plain text is mandatory — it is what a text-only
    client, a screen reader and the console backend all read — and the HTML
    part is an optional nicety."""

    to: str
    subject: str
    text: str
    html: str = ""


class EmailDeliveryError(AppError):
    """The backend could not hand the message to a mail server.

    A 502: the fault is upstream of this API. Not retried by anything
    automatic — the admin screen offers "Resend" for that — because a relay
    that refused once will usually refuse again until someone fixes it.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=502, retryable=False)


@runtime_checkable
class EmailSender(Protocol):
    """What every mail backend provides.

    ``configured`` says whether a message sent here actually leaves the
    machine. The console backend answers False, and that answer is what lets
    the admin screen show a copy-link button instead of promising an email
    that will never arrive.
    """

    backend: str
    configured: bool

    async def send(self, message: EmailMessage) -> None: ...
