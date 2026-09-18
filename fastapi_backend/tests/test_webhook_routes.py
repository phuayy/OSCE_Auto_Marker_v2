from __future__ import annotations

from app.domain.notifications import SUBSCRIBABLE_EVENT_TYPES

from tests.test_routes import build_test_client


def _authed(client) -> dict[str, str]:
    response = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _allow_private(client) -> None:
    """Relax the SSRF guard for the whole container.

    These tests use example.com URLs and must not depend on DNS; the guard
    itself is covered directly in test_webhook_url_guard.py. Both the container
    and the dispatcher are updated — the dispatcher re-validates at send time
    from its *own* settings, so patching only the container would leave delivery
    tests silently exercising the guard instead of the HTTP path.
    """
    container = client.app.state.container
    relaxed = container.settings.__class__(
        **{
            **{
                field: getattr(container.settings, field)
                for field in container.settings.__dataclass_fields__
            },
            "webhook_allow_private_urls": True,
        }
    )
    container.settings = relaxed
    container.webhook_dispatcher.settings = relaxed


def test_webhook_routes_require_authentication(tmp_path) -> None:
    client = build_test_client(tmp_path)
    assert client.get("/api/admin/webhooks").status_code == 401
    assert client.post("/api/admin/webhooks", json={"url": "https://example.com/h"}).status_code == 401
    assert client.delete("/api/admin/webhooks/anything").status_code == 401


def test_create_returns_the_secret_once_then_masks_it(tmp_path) -> None:
    """The full secret is unrecoverable after creation, so a leaked later
    response cannot be used to forge a signature."""
    client = build_test_client(tmp_path)
    _allow_private(client)
    headers = _authed(client)

    created = client.post(
        "/api/admin/webhooks",
        json={"url": "https://hooks.example.com/a", "description": "Slack relay"},
        headers=headers,
    )
    assert created.status_code == 201
    webhook = created.json()["webhook"]
    assert webhook["secret"]
    assert webhook["secretPreview"].startswith("...")
    assert webhook["secretPreview"].endswith(webhook["secret"][-4:])

    listed = client.get("/api/admin/webhooks", headers=headers).json()
    assert len(listed["webhooks"]) == 1
    assert "secret" not in listed["webhooks"][0]
    assert listed["webhooks"][0]["secretPreview"] == webhook["secretPreview"]
    # The catalogue drives the UI, so a new event type needs no frontend change.
    assert listed["eventTypes"] == list(SUBSCRIBABLE_EVENT_TYPES)


def test_unsafe_url_is_rejected_with_400(tmp_path) -> None:
    client = build_test_client(tmp_path)
    headers = _authed(client)

    response = client.post(
        "/api/admin/webhooks", json={"url": "http://169.254.169.254/latest/meta-data/"}, headers=headers
    )
    assert response.status_code == 400
    assert "private or loopback" in response.json()["error"]


def test_non_http_scheme_is_rejected(tmp_path) -> None:
    client = build_test_client(tmp_path)
    headers = _authed(client)

    response = client.post("/api/admin/webhooks", json={"url": "file:///etc/passwd"}, headers=headers)
    assert response.status_code == 400


def test_unknown_event_type_is_rejected(tmp_path) -> None:
    """Silently dropping it would leave the operator believing they subscribed."""
    client = build_test_client(tmp_path)
    _allow_private(client)
    headers = _authed(client)

    response = client.post(
        "/api/admin/webhooks",
        json={"url": "https://hooks.example.com/a", "eventTypes": ["not.a.real.event"]},
        headers=headers,
    )
    assert response.status_code == 400
    assert "Unknown event type" in response.json()["error"]


def test_wildcard_collapses_the_event_filter(tmp_path) -> None:
    client = build_test_client(tmp_path)
    _allow_private(client)
    headers = _authed(client)

    created = client.post(
        "/api/admin/webhooks",
        json={"url": "https://hooks.example.com/a", "eventTypes": ["*", "scoring.completed"]},
        headers=headers,
    )
    assert created.json()["webhook"]["eventTypes"] == ["*"]


def test_update_and_delete_lifecycle(tmp_path) -> None:
    client = build_test_client(tmp_path)
    _allow_private(client)
    headers = _authed(client)

    webhook_id = client.post(
        "/api/admin/webhooks", json={"url": "https://hooks.example.com/a"}, headers=headers
    ).json()["webhook"]["id"]

    updated = client.put(
        f"/api/admin/webhooks/{webhook_id}",
        json={
            "url": "https://hooks.example.com/b",
            "description": "moved",
            "eventTypes": ["clips.ready"],
            "active": False,
        },
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["webhook"]["url"] == "https://hooks.example.com/b"
    assert updated.json()["webhook"]["eventTypes"] == ["clips.ready"]
    assert updated.json()["webhook"]["active"] is False

    assert client.delete(f"/api/admin/webhooks/{webhook_id}", headers=headers).status_code == 200
    assert client.get("/api/admin/webhooks", headers=headers).json()["webhooks"] == []


def test_rotate_secret_issues_a_new_one(tmp_path) -> None:
    client = build_test_client(tmp_path)
    _allow_private(client)
    headers = _authed(client)

    created = client.post(
        "/api/admin/webhooks", json={"url": "https://hooks.example.com/a"}, headers=headers
    ).json()["webhook"]

    rotated = client.post(f"/api/admin/webhooks/{created['id']}/rotate-secret", headers=headers)
    assert rotated.status_code == 200
    assert rotated.json()["webhook"]["secret"] != created["secret"]


def test_missing_webhook_returns_404(tmp_path) -> None:
    client = build_test_client(tmp_path)
    headers = _authed(client)

    assert client.put(
        "/api/admin/webhooks/nope",
        json={"url": "https://hooks.example.com/a"},
        headers=headers,
    ).status_code in {400, 404}
    assert client.delete("/api/admin/webhooks/nope", headers=headers).status_code == 404
    assert client.get("/api/admin/webhooks/nope/deliveries", headers=headers).status_code == 404
    assert client.post("/api/admin/webhooks/nope/test", headers=headers).status_code == 404


def test_deliveries_are_listed_for_a_subscription(tmp_path) -> None:
    client = build_test_client(tmp_path)
    _allow_private(client)
    headers = _authed(client)

    webhook_id = client.post(
        "/api/admin/webhooks", json={"url": "https://hooks.example.com/a"}, headers=headers
    ).json()["webhook"]["id"]

    response = client.get(f"/api/admin/webhooks/{webhook_id}/deliveries", headers=headers)
    assert response.status_code == 200
    assert response.json()["deliveries"] == []


def test_test_endpoint_records_a_delivery_attempt(tmp_path) -> None:
    """A real (failing) delivery attempt, end to end through the route.

    Nothing is listening on port 9, so the connection is refused immediately —
    which is the point: the operator gets a definite answer and a log row
    explaining it, rather than silence.
    """
    client = build_test_client(tmp_path)
    _allow_private(client)
    headers = _authed(client)
    # One attempt only, so the test does not sit through retry backoff.
    client.app.state.container.webhook_dispatcher.settings = client.app.state.container.settings.__class__(
        **{
            **{
                field: getattr(client.app.state.container.settings, field)
                for field in client.app.state.container.settings.__dataclass_fields__
            },
            "webhook_max_attempts": 1,
        }
    )

    webhook_id = client.post(
        "/api/admin/webhooks",
        json={"url": "http://127.0.0.1:9/hook"},
        headers=headers,
    ).json()["webhook"]["id"]

    response = client.post(f"/api/admin/webhooks/{webhook_id}/test", headers=headers)
    assert response.status_code == 200
    assert response.json()["delivered"] is False

    deliveries = client.get(f"/api/admin/webhooks/{webhook_id}/deliveries", headers=headers).json()
    assert len(deliveries["deliveries"]) == 1
    row = deliveries["deliveries"][0]
    assert row["eventType"] == "webhook.test"
    assert row["status"] == "failed"
    # A transport failure, not the URL guard — the guard is relaxed here.
    assert row["statusCode"] is None
    assert "private or loopback" not in str(row["error"])

    # The failure is reflected on the subscription summary the UI renders.
    listed = client.get("/api/admin/webhooks", headers=headers).json()["webhooks"][0]
    assert listed["consecutiveFailures"] == 1
