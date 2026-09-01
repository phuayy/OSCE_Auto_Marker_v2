from __future__ import annotations

from enum import Enum


class NotificationType(str, Enum):
    """Event types a notification can announce.

    The value is the wire identifier: it is what a webhook subscriber filters
    on, what the ``X-OSCE-Event`` header carries, and what the browser sees on
    the SSE payload. Treat these strings as a public contract — renaming one
    silently breaks every registered subscriber's filter.

    ``str`` mixin so the value serialises directly to JSON and compares equal to
    a plain string, which keeps repository/DB code free of enum conversions.
    """

    SCORING_COMPLETED = "scoring.completed"
    CLIPS_READY = "clips.ready"
    SESSION_FAILED = "session.failed"
    # Emitted only by the "send test event" action on a webhook subscription.
    # Never persisted as a user-facing notification.
    WEBHOOK_TEST = "webhook.test"


# Types a user can subscribe a webhook to. WEBHOOK_TEST is excluded: it is
# delivered directly to the subscription under test, never fanned out.
SUBSCRIBABLE_EVENT_TYPES: tuple[str, ...] = (
    NotificationType.SCORING_COMPLETED.value,
    NotificationType.CLIPS_READY.value,
    NotificationType.SESSION_FAILED.value,
)

# Wildcard accepted in a subscription's event_types, meaning "every subscribable
# type" — including ones added after the subscription was created.
EVENT_TYPE_WILDCARD = "*"


def event_types_match(subscribed: list[str] | tuple[str, ...] | None, event_type: str) -> bool:
    """True when a subscription listening for ``subscribed`` wants ``event_type``.

    An empty/absent list means "everything", matching the wildcard. This is the
    single place the filter is decided, so the dispatcher and the API agree.
    """
    if not subscribed:
        return True
    if EVENT_TYPE_WILDCARD in subscribed:
        return True
    return event_type in subscribed


__all__ = [
    "EVENT_TYPE_WILDCARD",
    "NotificationType",
    "SUBSCRIBABLE_EVENT_TYPES",
    "event_types_match",
]
