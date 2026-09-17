"""The three messages this app sends, as plain text plus a minimal HTML twin.

No template engine: the messages are short, the variables are few, and a
dependency for two paragraphs is a maintenance cost with no return. Every
value that reaches the HTML part is escaped here, so a display name is never
markup.
"""

from __future__ import annotations

import html

from app.mail.base import EmailMessage

APP_NAME = "OSCE AI Marker"


def _html_document(title: str, paragraphs: list[str], *, link: str = "", button: str = "") -> str:
    body = "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)
    action = ""
    if link:
        safe_link = html.escape(link, quote=True)
        action = (
            f'<p><a href="{safe_link}" style="display:inline-block;padding:10px 18px;'
            'background:#0e7490;color:#ffffff;text-decoration:none;border-radius:8px;">'
            f"{html.escape(button or 'Open')}</a></p>"
            f'<p style="color:#64748b;font-size:12px;">If the button does not work, copy this address '
            f"into your browser:<br>{safe_link}</p>"
        )
    return (
        "<!doctype html><html><body style=\"font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        'color:#0f172a;line-height:1.5;">'
        f"<h2 style=\"margin:0 0 12px;\">{html.escape(title)}</h2>{body}{action}"
        f'<p style="color:#64748b;font-size:12px;">{html.escape(APP_NAME)}</p></body></html>'
    )


def invitation(*, to: str, display_name: str, invited_by: str, link: str, expires_hours: int) -> EmailMessage:
    greeting = f"Hello {display_name}," if display_name else "Hello,"
    inviter = f" by {invited_by}" if invited_by else ""
    text = "\n".join(
        [
            greeting,
            "",
            f"You have been invited{inviter} to mark OSCE assessments in {APP_NAME}.",
            "",
            "Open the link below to choose a password and activate your account:",
            link,
            "",
            f"The link works once and expires in {expires_hours} hours. If you were not expecting this, "
            "you can ignore this message.",
        ]
    )
    html_body = _html_document(
        f"You are invited to {APP_NAME}",
        [
            html.escape(greeting),
            f"You have been invited{html.escape(inviter)} to mark OSCE assessments in {html.escape(APP_NAME)}.",
            "Open the link below to choose a password and activate your account.",
            f"The link works once and expires in {expires_hours} hours. If you were not expecting this, "
            "you can ignore this message.",
        ],
        link=link,
        button="Activate account",
    )
    return EmailMessage(to=to, subject=f"Your {APP_NAME} account", text=text, html=html_body)


def password_reset(*, to: str, display_name: str, link: str, expires_minutes: int) -> EmailMessage:
    greeting = f"Hello {display_name}," if display_name else "Hello,"
    text = "\n".join(
        [
            greeting,
            "",
            f"Someone asked to reset the password for your {APP_NAME} account.",
            "Open the link below to choose a new one:",
            link,
            "",
            f"The link works once and expires in {expires_minutes} minutes. If you did not ask for this, "
            "ignore this message — your password stays as it is.",
        ]
    )
    html_body = _html_document(
        "Reset your password",
        [
            html.escape(greeting),
            f"Someone asked to reset the password for your {html.escape(APP_NAME)} account. "
            "Open the link below to choose a new one.",
            f"The link works once and expires in {expires_minutes} minutes. If you did not ask for this, "
            "ignore this message — your password stays as it is.",
        ],
        link=link,
        button="Choose a new password",
    )
    return EmailMessage(to=to, subject=f"Reset your {APP_NAME} password", text=text, html=html_body)


def password_changed(*, to: str, display_name: str) -> EmailMessage:
    greeting = f"Hello {display_name}," if display_name else "Hello,"
    text = "\n".join(
        [
            greeting,
            "",
            f"The password for your {APP_NAME} account was just changed, and every other "
            "signed-in session was ended.",
            "",
            "If this was you, there is nothing to do. If it was not, ask an administrator to "
            "disable the account and send you a new password-reset link.",
        ]
    )
    html_body = _html_document(
        "Your password was changed",
        [
            html.escape(greeting),
            f"The password for your {html.escape(APP_NAME)} account was just changed, and every other "
            "signed-in session was ended.",
            "If this was you, there is nothing to do. If it was not, ask an administrator to disable "
            "the account and send you a new password-reset link.",
        ],
    )
    return EmailMessage(to=to, subject=f"Your {APP_NAME} password was changed", text=text, html=html_body)
