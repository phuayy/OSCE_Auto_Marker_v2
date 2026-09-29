"""Audit 2026-09-29 finding 2: a body with no (or a false) Content-Length must
still be capped while it is being received, not only by its declaration.

``test_body_limit.py`` pins the declared-length fast path; these pin the
streaming count underneath it — chunked bodies, bodies spread over several
ASGI frames, and the upload-part route's own larger cap.
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from app.api.errors import http_error
from app.core.body_limit import MaxBodySizeMiddleware
from app.core.config import Settings
from app.main import build_app


class Body(BaseModel):
    # Module scope on purpose: under ``from __future__ import annotations`` a
    # function-local model cannot be resolved, and FastAPI would silently read
    # ``body`` as a query parameter instead.
    x: str


async def _echo(request):
    body = await request.body()
    return PlainTextResponse(str(len(body)))


def _client(max_bytes: int, part_max_bytes: int | None) -> TestClient:
    inner = Starlette(
        routes=[
            Route("/echo", _echo, methods=["POST"]),
            Route("/api/uploads/{upload_id}/parts/{part_number}", _echo, methods=["PUT"]),
        ]
    )
    return TestClient(MaxBodySizeMiddleware(inner, max_bytes=max_bytes, part_max_bytes=part_max_bytes))


def _frames(count: int, size: int = 256):
    # A generator body is sent chunked, with no Content-Length at all.
    for _ in range(count):
        yield b"x" * size


def test_a_chunked_body_under_the_cap_passes_through() -> None:
    response = _client(1024, 4096).post("/echo", content=_frames(3))
    assert response.status_code == 200
    assert response.text == "768"


def test_a_chunked_body_over_the_cap_is_refused_while_streaming() -> None:
    response = _client(1024, 4096).post("/echo", content=_frames(8))
    assert response.status_code == 413
    assert "error" in response.json()


def test_the_part_route_is_capped_at_its_own_larger_limit_when_chunked() -> None:
    client = _client(1024, 4096)
    ok = client.put("/api/uploads/u1/parts/1", content=_frames(8))  # 2048 > generic, < part
    assert ok.status_code == 200
    assert ok.text == "2048"
    refused = client.put("/api/uploads/u1/parts/1", content=_frames(20))  # 5120 > part
    assert refused.status_code == 413


def test_the_part_route_declared_length_uses_the_part_cap() -> None:
    client = _client(10, 4096)
    assert client.put("/api/uploads/u1/parts/1", content=b"x" * 1000).status_code == 200
    assert client.put("/api/uploads/u1/parts/1", content=b"x" * 5000).status_code == 413


def test_a_fastapi_json_route_answers_413_not_400_when_the_stream_overruns() -> None:
    """FastAPI turns any non-HTTPException raised while parsing a body into a
    400 "error parsing the body"; the overrun must surface as 413."""

    inner = FastAPI()

    @inner.post("/json")
    async def json_route(body: Body):
        return {"len": len(body.x)}

    client = TestClient(MaxBodySizeMiddleware(inner, max_bytes=1024, part_max_bytes=None))

    def payload():
        yield b'{"x":"'
        for _ in range(8):
            yield b"y" * 256
        yield b'"}'

    response = client.post("/json", content=payload(), headers={"content-type": "application/json"})
    assert response.status_code == 413


def test_a_route_that_converts_its_own_errors_still_answers_413() -> None:
    """The upload-part handler wraps its body read in ``except Exception`` and
    converts through ``http_error``; the overrun must not become a 500."""
    inner = FastAPI()

    @inner.put("/api/uploads/{upload_id}/parts/{part_number}")
    async def part(upload_id: str, part_number: int, request: Request):
        try:
            return {"len": len(await request.body())}
        except Exception as error:
            raise http_error(error) from error

    client = TestClient(MaxBodySizeMiddleware(inner, max_bytes=10, part_max_bytes=1024))
    response = client.put("/api/uploads/u1/parts/1", content=_frames(8))
    assert response.status_code == 413


def test_http_error_passes_an_http_exception_through_unchanged() -> None:
    original = HTTPException(status_code=413, detail="too big")
    assert http_error(original) is original


def test_the_real_app_wires_the_part_cap_from_settings(tmp_path) -> None:
    settings = Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        app_database_url="",
        database_url="",
        max_request_body_mb=1,
        upload_part_size_mb=8,
    )
    app = build_app(settings)
    wired = [m for m in app.user_middleware if m.cls is MaxBodySizeMiddleware]
    assert len(wired) == 1
    assert wired[0].kwargs["max_bytes"] == settings.max_request_body_bytes
    assert wired[0].kwargs["part_max_bytes"] == settings.upload_part_size_bytes
