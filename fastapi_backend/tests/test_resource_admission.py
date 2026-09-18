import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api.dependencies import require_expensive_operation
from app.api.routes import async_uploads, jobs, sessions
from app.core.exceptions import AppError
from app.core.rate_limit import FixedWindowRateLimiter
from app.domain.actors import Actor
from app.domain.users import UserRole, UserStatus
from app.schemas.uploads import InitiateUploadRequest
from app.services.container import create_container
from tests.test_routes import build_test_client
from tests.test_session_maintenance import _init, _settings


def request_for(user_id, ip='shared-nat'):
    request = Request({'type': 'http', 'headers': [], 'client': (ip, 1234)})
    request.state.auth_user = {'sub': user_id, 'username': user_id, 'role': 'marker'} if user_id else None
    return request


def test_expensive_budget_is_per_account_shared_across_actions_and_expires(monkeypatch):
    now = [100.0]
    monkeypatch.setattr('app.core.rate_limit.time.monotonic', lambda: now[0])
    container = SimpleNamespace(expensive_operation_rate_limiter=FixedWindowRateLimiter(max_attempts=1, window_seconds=3600))
    require_expensive_operation(request_for('a'), container)
    require_expensive_operation(request_for('b'), container)
    with pytest.raises(AppError) as rejected:
        require_expensive_operation(request_for('a', 'different-ip'), container)
    assert rejected.value.status_code == 429
    with pytest.raises(HTTPException) as unauthenticated:
        require_expensive_operation(request_for(None), container)
    assert unauthenticated.value.status_code == 401
    now[0] += 3600
    require_expensive_operation(request_for('a'), container)


def test_all_expensive_entry_points_share_the_guard():
    expected = {
        '/sessions/{session_id}/rerun', '/sessions/{session_id}/auto-crop',
        '/sessions/{session_id}/process', '/sessions/{session_id}/clips/manual',
        '/sessions/{session_id}/clips/{clip_id}/recrop', '/sessions/{session_id}/clips/{clip_id}/assess',
        '/jobs/{job_id}/rerun', '/uploads/initiate',
    }
    guarded = {
        route.path for router in (sessions.router, jobs.router, async_uploads.router)
        for route in router.routes
        if any(dependency.call is require_expensive_operation for dependency in route.dependant.dependencies)
    }
    assert guarded == expected


def test_http_budget_rejects_before_dispatch(tmp_path, monkeypatch):
    client = build_test_client(tmp_path)
    container = client.app.state.container
    container.expensive_operation_rate_limiter = FixedWindowRateLimiter(max_attempts=1, window_seconds=3600)
    token = client.post('/api/auth/login', json={'username': 'admin', 'password': 'admin'}).json()['token']
    headers = {'Authorization': f'Bearer {token}'}
    asyncio.run(container.sessions.write({'id': 's1', 'name': 'Session'}))
    calls = []

    async def dispatch(session_id):
        calls.append(session_id)
        return {'ok': True}

    monkeypatch.setattr(container.session_maintenance, 'rerun_session', dispatch)
    monkeypatch.setattr(container.session_maintenance, 'start_processing', dispatch)
    assert client.post('/api/sessions/s1/rerun', headers=headers).status_code == 200
    assert client.post('/api/sessions/s1/process', headers=headers).status_code == 429
    assert calls == ['s1']


def upload_request():
    return InitiateUploadRequest(files=[
        {'kind': 'video', 'originalName': 'video.mp4', 'mimeType': 'video/mp4', 'sizeBytes': 1},
        {'kind': 'caseStudy', 'originalName': 'case.pdf', 'mimeType': 'application/pdf', 'sizeBytes': 1},
    ])


async def create_actor(container, name):
    user = await container.users.create(username=name, email=None, display_name=name,
        role=UserRole.MARKER, status=UserStatus.ACTIVE, password_hash=None)
    return Actor(user_id=user.id, username=name)


def test_upload_cap_is_atomic_across_containers_and_accounts(tmp_path):
    async def run():
        settings = replace(_settings(tmp_path), max_concurrent_uploads_per_user=1)
        first, second = create_container(settings), create_container(settings)
        await _init(first)
        await _init(second)
        try:
            actor = await create_actor(first, 'a')
            other = await create_actor(first, 'b')
            results = await asyncio.gather(
                first.async_uploads.initiate(upload_request(), actor=actor),
                second.async_uploads.initiate(upload_request(), actor=actor), return_exceptions=True,
            )
            assert sum(isinstance(result, dict) for result in results) == 1
            failures = [result for result in results if isinstance(result, AppError)]
            assert len(failures) == 1 and failures[0].status_code == 429
            assert len(await first.async_uploads.repository.read_all()) == 1
            assert len(await first.jobs.list_jobs()) == 1
            await second.async_uploads.initiate(upload_request(), actor=other)
            assert len(await first.async_uploads.repository.read_all()) == 2
        finally:
            await second.shutdown()
            await first.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize(('status', 'expired', 'blocked'), [
    ('initiated', False, True), ('uploading', False, True), ('assembling', False, True),
    ('assembling', True, True), ('initiated', True, False), ('uploading', True, False),
    ('failed', False, True), ('failed', True, False),
    ('committed', False, False), ('aborted', False, False), ('expired', False, False),
])
def test_upload_capacity_follows_persisted_lifecycle(tmp_path, status, expired, blocked):
    async def run():
        container = create_container(replace(_settings(tmp_path), max_concurrent_uploads_per_user=1))
        await _init(container)
        try:
            actor = await create_actor(container, 'a')
            first = await container.async_uploads.initiate(upload_request(), actor=actor)
            repo = container.async_uploads.repository
            upload = await repo.read(first['uploadId'])
            upload['status'] = status
            if expired:
                upload['expiresAt'] = '2000-01-01T00:00:00Z'
            await repo.write(upload)
            if blocked:
                with pytest.raises(AppError) as rejected:
                    await container.async_uploads.initiate(upload_request(), actor=actor)
                assert rejected.value.status_code == 429
            else:
                await container.async_uploads.initiate(upload_request(), actor=actor)
        finally:
            await container.shutdown()
    asyncio.run(run())


def test_failed_initiation_does_not_consume_capacity(tmp_path, monkeypatch):
    async def run():
        container = create_container(replace(_settings(tmp_path), max_concurrent_uploads_per_user=1))
        await _init(container)
        try:
            actor = await create_actor(container, 'a')
            original = container.async_uploads.repository.write

            async def fail(upload):
                await original(upload)
                raise RuntimeError('injected failure')

            with monkeypatch.context() as patch:
                patch.setattr(container.async_uploads.repository, 'write', fail)
                with pytest.raises(RuntimeError, match='injected'):
                    await container.async_uploads.initiate(upload_request(), actor=actor)
            assert await container.async_uploads.repository.read_all() == []
            await container.async_uploads.initiate(upload_request(), actor=actor)
        finally:
            await container.shutdown()
    asyncio.run(run())
