from __future__ import annotations

import asyncio
import json
import time

from app.core.config import Settings
from app.database.orm import OrmDatabase
from app.domain.notifications import NotificationType
from app.repositories.webhook_repository import WebhookRepository
from app.services.webhook_dispatcher import (
    DELIVERY_HEADER,
    EVENT_HEADER,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    WebhookDispatcher,
    build_signature,
    verify_signature,
)


class _StubResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


class _StubClient:
    """Stands in for httpx.AsyncClient.

    Records every request and replays a scripted sequence of outcomes, so retry
    and signing behaviour can be asserted without a real HTTP server.
    """

    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[dict[str, object]] = []

    async def __aenter__(self) -> "_StubClient":
        return self

    async def __aexit__(self, *_exc) -> bool:
        return False

    async def post(self, url, content=None, headers=None):
        self.requests.append(
            {"url": url, "body": bytes(content or b"").decode("utf-8"), "headers": dict(headers or {})}
        )
        outcome = self.outcomes.pop(0) if self.outcomes else _StubResponse(200)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _settings(**overrides) -> Settings:
    defaults = {
        "ffmpeg_bin": "ffmpeg",
        "ffprobe_bin": "ffprobe",
        "scorer_python_bin": "python",
        # Tests use example.com URLs and must not depend on DNS; the guard is
        # exercised directly in test_webhook_url_guard.py.
        "webhook_allow_private_urls": True,
    }
    return Settings(**{**defaults, **overrides})


def _dispatcher(tmp_path, client: _StubClient, **setting_overrides):
    repository = WebhookRepository(OrmDatabase(tmp_path / "app.sqlite3"))
    dispatcher = WebhookDispatcher(
        _settings(**setting_overrides),
        repository,
        client_factory=lambda: client,
    )
    # No backoff sleeping in tests — retry *ordering* is what matters here.
    dispatcher._backoff_seconds = lambda attempt: 0.0
    return dispatcher, repository


_NOTIFICATION = {
    "id": "notif-1",
    "sessionId": "session-1",
    "title": "Scoring complete",
    "body": "Results are ready.",
    "createdAt": "2026-01-01T00:00:00+00:00",
}


# --- signing ---------------------------------------------------------------


def test_signature_round_trips_through_the_reference_verifier() -> None:
    timestamp = int(time.time())
    body = json.dumps({"hello": "world"})
    header = build_signature("s3cret", timestamp, body)

    assert verify_signature("s3cret", header, body) is True


def test_signature_rejects_a_tampered_body() -> None:
    timestamp = int(time.time())
    header = build_signature("s3cret", timestamp, '{"amount":1}')

    assert verify_signature("s3cret", header, '{"amount":9999}') is False


def test_signature_rejects_the_wrong_secret() -> None:
    timestamp = int(time.time())
    body = "{}"
    header = build_signature("real-secret", timestamp, body)

    assert verify_signature("other-secret", header, body) is False


def test_signature_rejects_a_replayed_delivery() -> None:
    """The timestamp is inside the signed material, so an old capture expires."""
    old = int(time.time()) - 3600
    body = "{}"
    header = build_signature("s3cret", old, body)

    assert verify_signature("s3cret", header, body, tolerance_seconds=300) is False
    # ...and is still valid to a receiver that chooses a wider window.
    assert verify_signature("s3cret", header, body, tolerance_seconds=7200) is True


def test_signature_rejects_malformed_headers() -> None:
    for header in ["", "garbage", "t=abc,v1=deadbeef", "v1=deadbeef", "t=123"]:
        assert verify_signature("s3cret", header, "{}") is False


# --- delivery --------------------------------------------------------------


def test_successful_delivery_sends_signed_verifiable_request(tmp_path) -> None:
    async def scenario() -> None:
        client = _StubClient([_StubResponse(200)])
        dispatcher, repository = _dispatcher(tmp_path, client)
        subscription = await repository.create(url="https://hooks.example.com/a", event_types=[])

        delivered = await dispatcher.deliver(
            subscription, NotificationType.SCORING_COMPLETED.value, _NOTIFICATION
        )

        assert delivered is True
        assert len(client.requests) == 1
        request = client.requests[0]
        headers = request["headers"]
        assert headers[EVENT_HEADER] == "scoring.completed"
        assert headers[DELIVERY_HEADER] == "notif-1"
        assert headers["Content-Type"] == "application/json"
        assert TIMESTAMP_HEADER in headers
        # The subscriber's own verification must pass against the real request.
        assert verify_signature(subscription["secret"], headers[SIGNATURE_HEADER], request["body"]) is True
        # And the body is the documented payload shape.
        assert json.loads(request["body"]) == {
            "id": "notif-1",
            "type": "scoring.completed",
            "createdAt": "2026-01-01T00:00:00+00:00",
            "sessionId": "session-1",
            "title": "Scoring complete",
            "body": "Results are ready.",
        }

    asyncio.run(scenario())


def test_server_error_is_retried_up_to_the_limit(tmp_path) -> None:
    async def scenario() -> None:
        client = _StubClient([_StubResponse(500), _StubResponse(503), _StubResponse(200)])
        dispatcher, repository = _dispatcher(tmp_path, client, webhook_max_attempts=3)
        subscription = await repository.create(url="https://hooks.example.com/b")

        delivered = await dispatcher.deliver(subscription, "scoring.completed", _NOTIFICATION)

        assert delivered is True
        assert len(client.requests) == 3

        deliveries = await repository.list_deliveries(subscription["id"])
        assert [row["status"] for row in deliveries] == ["succeeded", "failed", "failed"]
        assert [row["attempt"] for row in deliveries] == [3, 2, 1]

    asyncio.run(scenario())


def test_client_error_is_not_retried(tmp_path) -> None:
    """A 404/403 means the endpoint understood and refused; retrying only
    amplifies load against someone else's server."""

    async def scenario() -> None:
        client = _StubClient([_StubResponse(404), _StubResponse(200)])
        dispatcher, repository = _dispatcher(tmp_path, client, webhook_max_attempts=3)
        subscription = await repository.create(url="https://hooks.example.com/c")

        delivered = await dispatcher.deliver(subscription, "scoring.completed", _NOTIFICATION)

        assert delivered is False
        assert len(client.requests) == 1

    asyncio.run(scenario())


def test_rate_limit_is_retried(tmp_path) -> None:
    async def scenario() -> None:
        client = _StubClient([_StubResponse(429), _StubResponse(200)])
        dispatcher, repository = _dispatcher(tmp_path, client, webhook_max_attempts=3)
        subscription = await repository.create(url="https://hooks.example.com/d")

        assert await dispatcher.deliver(subscription, "scoring.completed", _NOTIFICATION) is True
        assert len(client.requests) == 2

    asyncio.run(scenario())


def test_transport_error_is_retried_then_recorded(tmp_path) -> None:
    async def scenario() -> None:
        client = _StubClient([ConnectionError("connection refused"), ConnectionError("still down")])
        dispatcher, repository = _dispatcher(tmp_path, client, webhook_max_attempts=2)
        subscription = await repository.create(url="https://hooks.example.com/e")

        assert await dispatcher.deliver(subscription, "scoring.completed", _NOTIFICATION) is False
        assert len(client.requests) == 2

        deliveries = await repository.list_deliveries(subscription["id"])
        assert all(row["status"] == "failed" for row in deliveries)
        assert "connection refused" in str(deliveries[-1]["error"])

        # The subscription summary reflects the failure streak for the UI.
        stored = await repository.get(subscription["id"])
        assert stored["consecutiveFailures"] == 2

    asyncio.run(scenario())


def test_unsafe_url_is_refused_at_send_time(tmp_path) -> None:
    """A URL stored while private targets were allowed must not be delivered to
    after the setting is tightened."""

    async def scenario() -> None:
        client = _StubClient([_StubResponse(200)])
        dispatcher, repository = _dispatcher(tmp_path, client, webhook_allow_private_urls=False)
        subscription = await repository.create(url="http://127.0.0.1:9/hook")

        assert await dispatcher.deliver(subscription, "scoring.completed", _NOTIFICATION) is False
        assert client.requests == []

        deliveries = await repository.list_deliveries(subscription["id"])
        assert deliveries[0]["status"] == "failed"
        assert "private or loopback" in str(deliveries[0]["error"])

    asyncio.run(scenario())


# --- fan-out ---------------------------------------------------------------


def test_dispatch_only_reaches_subscriptions_matching_the_event(tmp_path) -> None:
    async def scenario() -> None:
        client = _StubClient([_StubResponse(200)] * 5)
        dispatcher, repository = _dispatcher(tmp_path, client)

        wanted = await repository.create(
            url="https://hooks.example.com/wanted", event_types=["scoring.completed"]
        )
        await repository.create(url="https://hooks.example.com/other", event_types=["clips.ready"])
        wildcard = await repository.create(url="https://hooks.example.com/all", event_types=["*"])
        catch_all = await repository.create(url="https://hooks.example.com/empty", event_types=[])
        await repository.create(
            url="https://hooks.example.com/off", event_types=["scoring.completed"], active=False
        )

        delivered = await dispatcher.dispatch("scoring.completed", _NOTIFICATION)

        assert delivered == 3
        urls = {request["url"] for request in client.requests}
        assert urls == {wanted["url"], wildcard["url"], catch_all["url"]}

    asyncio.run(scenario())


def test_dispatch_with_no_subscriptions_is_a_noop(tmp_path) -> None:
    async def scenario() -> None:
        client = _StubClient([])
        dispatcher, _repository = _dispatcher(tmp_path, client)

        assert await dispatcher.dispatch("scoring.completed", _NOTIFICATION) == 0
        assert client.requests == []

    asyncio.run(scenario())


def test_one_failing_endpoint_does_not_stop_the_others(tmp_path) -> None:
    async def scenario() -> None:
        # Deliveries run concurrently, so outcome order is not deterministic;
        # every attempt fails except the last one available.
        client = _StubClient([_StubResponse(200), _StubResponse(200), _StubResponse(500)])
        dispatcher, repository = _dispatcher(tmp_path, client, webhook_max_attempts=1)
        for index in range(3):
            await repository.create(url=f"https://hooks.example.com/{index}")

        delivered = await dispatcher.dispatch("scoring.completed", _NOTIFICATION)

        assert delivered == 2
        assert len(client.requests) == 3

    asyncio.run(scenario())
