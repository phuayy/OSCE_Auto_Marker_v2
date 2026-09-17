"""Outbound email: one contract, one backend per deployment.

The app sends exactly three kinds of message — an invitation, a password-reset
link and a "your password was changed" notice — and every deployment has a
different way to deliver them: an institutional SMTP relay, nothing at all on a
developer's laptop. So the transport sits behind :class:`EmailSender`, the
messages are built by :mod:`app.mail.templates`, and
:func:`create_email_sender` picks the backend from ``EMAIL_BACKEND``, exactly
as ``app/storage`` does for object storage.
"""

from app.mail.base import EmailDeliveryError, EmailMessage, EmailSender
from app.mail.factory import create_email_sender

__all__ = ["EmailDeliveryError", "EmailMessage", "EmailSender", "create_email_sender"]
