from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Any

from app.core.config import Settings
from app.core.logging_utils import log_context
from app.core.webhook_url import WebhookUrlError, validate_webhook_url
from app.repositories.webhook_repository import WebhookRepository


logger = logging.getLogger(__name__)

# Header names. Prefixed so they cannot collide with anything a proxy adds.
SIGNATURE_HEADER = "X-OSCE-Signature"
EVENT_HEADER = "X-OSCE-Event"
DELIVERY_HEADER = "X-OSCE-Delivery"
TIMESTAMP_HEADER = "X-OSCE-Timestamp"

# Longest error string kept in the delivery log. A misconfigured endpoint can
# return a full HTML page; storing all of it would bloat the log for no benefit.
_MAX_ERROR_CHARS = 500


def build_signature(secret: str, timestamp: int, body: str) -> str:
    """HMAC-SHA256 over ``"{timestamp}.{body}"``, formatted ``t=...,v1=...``.

    The timestamp is inside the signed material, not merely alongside it: signing
    the body alone would let anyone who captured one delivery replay it forever,
    because the signature would stay valid. With the timestamp bound in, a
    receiver can reject anything older than its tolerance and the attacker cannot
    re-sign a fresher one without the secret.

    ``v1`` names the scheme so a future algorithm change can be rolled out by
    sending both versions during a transition.
    """
    payload = f"{timestamp}.{body}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify_signature(
    secret: str,
    header_value: str,
    body: str,
    *,
    tolerance_seconds: int = 300,
    now: float | None = None,
) -> bool:
    """Reference verifier — the check a subscriber should implement.

    Exists in this codebase so the contract is executable rather than prose: the
    unit tests verify real deliveries with it, which means the documented
    algorithm and the shipped one cannot drift apart.
    """
    parts = dict(
        piece.split("=", 1)
        for piece in str(header_value or "").split(",")
        if "=" in piece
    )
    raw_timestamp = parts.get("t")
    provided = parts.get("v1")
    if not raw_timestamp or not provided:
        return False
    try:
        timestamp = int(raw_timestamp)
    except (TypeError, ValueError):
        return False

    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        return False

    expected = hmac.new(
        secret.encode("utf-8"), f"{timestamp}.{body}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    # Constant-time: a plain == leaks how many leading bytes matched.
    return hmac.compare_digest(expected, provided)


def _is_retryable(status_code: int) -> bool:
    """Retry 5xx and 429 only.

    A 4xx (other than 429) means the endpoint understood us and refused — a bad
    path, a rejected signature, a revoked route. Retrying that is guaranteed to
    fail again and just amplifies load against someone else's server.
    """
    return status_code >= 500 or status_code == 429


class WebhookDispatcher:
    """Delivers notification events to registered HTTP endpoints.

    Delivery is deliberately *outside* the caller's critical path: the pipeline
    that finished scoring must not slow down, or fail, because someone's Slack
    relay is down. :class:`~app.services.notification_service.NotificationService`
    therefore spawns dispatch as a tracked background task, and every failure
    mode here terminates in a log line plus a delivery-log row.
    """

    def __init__(
        self,
        settings: Settings,
        repository: WebhookRepository,
        *,
        client_factory: Any | None = None,
    ) -> None:
        self.settings = settings
        self.repository = repository
        # Injectable so tests can drive delivery against a stub transport
        # instead of standing up a real HTTP server.
        self._client_factory = client_factory

    def _new_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        import httpx

        return httpx.AsyncClient(
            timeout=self.settings.webhook_timeout_seconds,
            # A webhook target that answers with a redirect is not a target we
            # should chase: following it is another SSRF vector, since the
            # destination is chosen by the remote server, not the operator.
            follow_redirects=False,
        )

    @staticmethod
    def build_payload(event_type: str, notification: dict[str, Any]) -> dict[str, Any]:
        """The JSON document POSTed to subscribers.

        Flat and stable by design — subscribers parse this, so it is an API.
        """
        return {
            "id": notification.get("id"),
            "type": event_type,
            "createdAt": notification.get("createdAt"),
            "sessionId": notification.get("sessionId"),
            "title": notification.get("title"),
            "body": notification.get("body"),
        }

    async def dispatch(self, event_type: str, notification: dict[str, Any]) -> int:
        """Deliver ``notification`` to every active subscription for the event.

        Returns the number of subscriptions that accepted it. Subscriptions are
        delivered to concurrently — one slow endpoint must not delay the others.
        """
        try:
            subscriptions = await self.repository.list_active_for_event(event_type)
        except Exception:
            logger.exception("Could not load webhook subscriptions for %s.", event_type)
            return 0
        if not subscriptions:
            return 0

        results = await asyncio.gather(
            *(self.deliver(subscription, event_type, notification) for subscription in subscriptions),
            return_exceptions=True,
        )
        delivered = 0
        for result in results:
            if isinstance(result, Exception):
                logger.error("Webhook delivery raised unexpectedly.", exc_info=result)
            elif result:
                delivered += 1
        return delivered

    async def deliver(
        self,
        subscription: dict[str, Any],
        event_type: str,
        notification: dict[str, Any],
    ) -> bool:
        """POST one event to one subscription, retrying transient failures.

        Every attempt is logged to the delivery table whether it succeeds or
        fails, so an operator can see exactly what happened without server
        access. Returns True once the endpoint answers 2xx.
        """
        subscription_id = str(subscription.get("id") or "")
        secret = str(subscription.get("secret") or "")
        url = str(subscription.get("url") or "")
        context = log_context(subscription_id, "webhook_delivery", event_type=event_type)

        payload = self.build_payload(event_type, notification)
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        max_attempts = max(1, self.settings.webhook_max_attempts)

        for attempt in range(1, max_attempts + 1):
            # Re-validate before *every* attempt, not just once before the
            # loop: the safety of a URL is a property of what it resolves to
            # right now, and retries are exactly where that can change under
            # us — the backoff between attempts is real wall-clock time for a
            # hostname to rebind to a private address, and the allow-private
            # setting may itself have been tightened mid-run. This does not
            # close the gap fully (the resolution here and the one httpx does
            # to actually connect are still two separate lookups — see
            # `validate_webhook_url`'s docstring); it only shrinks the window
            # an attacker gets from "one validation, many deliveries" to "one
            # validation per delivery".
            try:
                validate_webhook_url(url, allow_private=self.settings.webhook_allow_private_urls)
            except WebhookUrlError as error:
                await self.repository.record_delivery(
                    subscription_id=subscription_id,
                    notification_id=notification.get("id"),
                    event_type=event_type,
                    attempt=attempt,
                    status="failed",
                    error=str(error)[:_MAX_ERROR_CHARS],
                )
                logger.warning("Refusing webhook delivery to unsafe URL: %s", error, extra=context)
                return False

            timestamp = int(time.time())
            headers = {
                "Content-Type": "application/json",
                "User-Agent": self.settings.webhook_user_agent,
                SIGNATURE_HEADER: build_signature(secret, timestamp, body),
                TIMESTAMP_HEADER: str(timestamp),
                EVENT_HEADER: event_type,
                DELIVERY_HEADER: str(notification.get("id") or ""),
            }
            started = time.monotonic()
            status_code: int | None = None
            error: str | None = None

            try:
                async with self._new_client() as client:
                    response = await client.post(url, content=body.encode("utf-8"), headers=headers)
                status_code = int(getattr(response, "status_code", 0))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — every transport error is a failed attempt
                error = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR_CHARS]

            duration_ms = int((time.monotonic() - started) * 1000)
            succeeded = status_code is not None and 200 <= status_code < 300
            if not succeeded and error is None:
                error = f"Endpoint returned HTTP {status_code}."

            await self.repository.record_delivery(
                subscription_id=subscription_id,
                notification_id=notification.get("id"),
                event_type=event_type,
                attempt=attempt,
                status="succeeded" if succeeded else "failed",
                status_code=status_code,
                error=None if succeeded else error,
                duration_ms=duration_ms,
            )

            if succeeded:
                logger.info(
                    "Webhook delivered to %s (HTTP %s, attempt %d).",
                    url,
                    status_code,
                    attempt,
                    extra=context,
                )
                return True

            transport_failure = status_code is None
            if not transport_failure and not _is_retryable(status_code or 0):
                logger.warning(
                    "Webhook rejected by %s (HTTP %s); not retrying.",
                    url,
                    status_code,
                    extra=context,
                )
                return False

            if attempt >= max_attempts:
                logger.warning(
                    "Webhook to %s failed after %d attempt(s): %s", url, attempt, error, extra=context
                )
                return False

            await asyncio.sleep(self._backoff_seconds(attempt))

        return False

    def _backoff_seconds(self, attempt: int) -> float:
        """Equal-jitter exponential backoff, shared with the job queue.

        Reused rather than reimplemented so retry behaviour is consistent across
        the app and tunable in one place.
        """
        from app.services.job_queue_service import compute_retry_backoff_seconds

        return compute_retry_backoff_seconds(attempt + 1)
