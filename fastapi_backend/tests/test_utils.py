from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import Any

import pytest

from app.core.asyncio_compat import configure_windows_signal_compatibility
from app.core.config import Settings, resolve_binary_from_candidates
from app.core.process import CommandRunner
from app.core.exceptions import AppError
from app.core.security import build_auth_payload, sign_payload, verify_signed_token
from app.core.utils import sanitize_file_name
from app.pipeline.media import MediaPipeline
from app.pipeline.scoring import ScoringPipeline
from app.services.job_queue_service import JobQueueService
from app.services.storage_service import LocalObjectStorageService
from tests.fixtures.session_store import SessionUpdateMixin


class CapturingEvents:
    def __init__(self) -> None:
        self.items: list[tuple[str, str, dict[str, Any]]] = []

    async def publish(self, session_id: str, event_name: str, payload: dict[str, Any]) -> None:
        self.items.append((session_id, event_name, payload))


class InMemorySessions(SessionUpdateMixin):
    def __init__(self, session: dict[str, Any]) -> None:
        self.session = dict(session)

    async def read(self, session_id: str) -> dict[str, Any]:
        if self.session.get("id") != session_id:
            raise FileNotFoundError(session_id)
        return dict(self.session)

    async def write(self, session: dict[str, Any]) -> None:
        self.session = dict(session)


def test_sanitize_file_name_preserves_extension_and_limits_base() -> None:
    assert sanitize_file_name("My OSCE Video!!.MP4") == "My-OSCE-Video.mp4"
    assert sanitize_file_name("###.pdf") == "file.pdf"


def test_srt_to_vtt_conversion() -> None:
    raw = "1\n00:00:01,000 --> 00:00:02,250\nHello\n"
    assert MediaPipeline.convert_srt_text_to_vtt_text(raw).startswith("WEBVTT\n\n1")
    assert "00:00:01.000 --> 00:00:02.250" in MediaPipeline.convert_srt_text_to_vtt_text(raw)


def test_auth_token_sign_verify_and_expiry() -> None:
    payload, _expires_at = build_auth_payload("admin", 60)
    token = sign_payload(payload, "secret")
    assert verify_signed_token(token, "secret")["username"] == "admin"
    assert verify_signed_token(token, "wrong") is None

    expired = {"username": "admin", "issuedAt": 1, "expiresAt": 1, "tokenId": "x"}
    assert verify_signed_token(sign_payload(expired, "secret"), "secret") is None


def test_score_staleness_rules() -> None:
    assert ScoringPipeline.should_refresh_score_payload({}) is True
    assert ScoringPipeline.should_refresh_audio_professionalism_payload({"schema": "old"}) is True
    assert ScoringPipeline.should_refresh_communication_payload({"schema": "communication-scoring-v1"}) is True

    valid_audio = {"schema": "audio-professionalism-v1", "metrics": {}}
    assert ScoringPipeline.should_refresh_audio_professionalism_payload(valid_audio) is False


def test_binary_resolution_uses_project_candidate_when_plain_command_is_not_on_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("PATH", "")
    candidate = tmp_path / "Scripts" / "whisperx.exe"
    candidate.parent.mkdir(parents=True, exist_ok=True)
    candidate.write_text("", encoding="utf-8")

    resolved = resolve_binary_from_candidates("whisperx", "whisperx", [candidate])

    assert resolved == str(candidate)


def test_windows_signal_compatibility_maps_missing_sigquit(monkeypatch) -> None:
    if sys.platform != "win32":
        pytest.skip("Windows-only signal compatibility")

    monkeypatch.delattr(signal, "SIGQUIT", raising=False)
    monkeypatch.setattr(signal, "SIGBREAK", signal.SIGTERM, raising=False)
    monkeypatch.setattr(
        asyncio.AbstractEventLoop,
        "add_signal_handler",
        asyncio.AbstractEventLoop.add_signal_handler,
    )

    configure_windows_signal_compatibility()

    assert signal.SIGQUIT == signal.SIGBREAK
    loop = asyncio.new_event_loop()
    try:
        assert loop.add_signal_handler(signal.SIGINT, lambda: None) is None
    finally:
        loop.close()


def test_command_runner_streams_subprocess_output(tmp_path) -> None:
    runner = CommandRunner(tmp_path)
    seen: list[tuple[str, str]] = []

    async def on_output(stream: str, text: str) -> None:
        seen.append((stream, text.strip()))

    result = asyncio.run(
        runner.run(
            sys.executable,
            ["-c", "print('runner-ok')"],
            "Command runner smoke test",
            on_output=on_output,
        )
    )

    assert result.stdout == "runner-ok"
    assert ("stdout", "runner-ok") in seen


def test_job_sync_clears_session_error_when_requeued(tmp_path) -> None:
    sessions = InMemorySessions({"id": "session-1", "status": "failed", "error": "previous failure"})
    service = JobQueueService(
        Settings(root_dir=tmp_path, backend_root=tmp_path),
        repository=object(),
        events=object(),
        sessions=sessions,
        storage=object(),
    )

    asyncio.run(service._sync_session_job({"id": "job-1", "sessionId": "session-1", "status": "queued"}))

    assert sessions.session["status"] == "queued"
    assert sessions.session["error"] is None


def test_whisperx_cuda_request_falls_back_to_cpu_when_cuda_unavailable(tmp_path, monkeypatch) -> None:
    settings = Settings(root_dir=tmp_path, backend_root=tmp_path, whisperx_device="cuda")
    events = CapturingEvents()
    media = MediaPipeline(settings, runner=object(), events=events, auth=object())
    monkeypatch.setattr(MediaPipeline, "_cuda_available", staticmethod(lambda: False))

    device = asyncio.run(media._resolve_whisperx_device("session-1"))

    assert device == "cpu"
    assert any("falling back to CPU" in item[2]["message"] for item in events.items)


def test_local_storage_assembles_parts_and_storage_ref(tmp_path) -> None:
    settings = Settings(root_dir=tmp_path, backend_root=tmp_path, upload_part_size_mb=1)
    storage = LocalObjectStorageService(settings)
    asyncio.run(storage.ensure_layout())
    prepared = asyncio.run(
        storage.prepare_upload_file(
            upload_id="u1",
            session_id="s1",
            file_id="f1",
            kind="video",
            original_name="Station Video.mp4",
            mime_type="video/mp4",
            size_bytes=6,
            checksum_sha256=None,
        )
    )
    upload = {
        "id": "u1",
        "status": "initiated",
        "files": [
            {
                "fileId": "f1",
                "kind": "video",
                "key": prepared.key,
                "mimeType": "video/mp4",
                "sizeBytes": 6,
                "parts": [],
                "status": "initiated",
            }
        ],
    }
    asyncio.run(storage.put_part(upload, "f1", 1, b"abc"))
    asyncio.run(storage.put_part(upload, "f1", 2, b"def"))
    storage_ref = asyncio.run(storage.complete_file(upload, upload["files"][0]))
    assert storage_ref["status"] == "committed"
    assert storage_ref["key"].endswith("/video/Station-Video.mp4")
    assert "localPath" in storage_ref


def test_local_storage_completion_reuses_existing_final_object(tmp_path) -> None:
    settings = Settings(root_dir=tmp_path, backend_root=tmp_path, upload_part_size_mb=1)
    storage = LocalObjectStorageService(settings)
    asyncio.run(storage.ensure_layout())
    prepared = asyncio.run(
        storage.prepare_upload_file(
            upload_id="u1",
            session_id="s1",
            file_id="f1",
            kind="video",
            original_name="Station Video.mp4",
            mime_type="video/mp4",
            size_bytes=6,
            checksum_sha256=None,
        )
    )
    file_record = {
        "fileId": "f1",
        "kind": "video",
        "key": prepared.key,
        "mimeType": "video/mp4",
        "sizeBytes": 6,
        "parts": [],
        "status": "initiated",
    }
    upload = {"id": "u1", "status": "initiated", "files": [file_record]}

    asyncio.run(storage.put_part(upload, "f1", 1, b"abc"))
    asyncio.run(storage.put_part(upload, "f1", 2, b"def"))
    first_ref = asyncio.run(storage.complete_file(upload, file_record))
    asyncio.run(storage.abort_upload(upload))

    stale_file_record = {
        **file_record,
        "status": "uploading",
        "uploadedBytes": 6,
        "parts": file_record["parts"],
    }
    stale_file_record.pop("storageRef", None)
    stale_upload = {"id": "u1", "status": "uploading", "files": [stale_file_record]}

    retry_ref = asyncio.run(storage.complete_file(stale_upload, stale_file_record))

    assert retry_ref["checksumSha256"] == first_ref["checksumSha256"]
    assert retry_ref["localPath"] == first_ref["localPath"]
    assert stale_file_record["status"] == "committed"


def test_atomic_replace_retries_transient_permission_error(tmp_path, monkeypatch) -> None:
    from app.core import utils

    src = tmp_path / "src.tmp"
    dst = tmp_path / "dst.json"
    src.write_text("payload", encoding="utf-8")

    calls = {"n": 0}
    real_replace = os.replace

    def flaky_replace(a: Any, b: Any) -> None:
        calls["n"] += 1
        if calls["n"] < 3:  # deny the first two attempts like an AV scan would
            raise PermissionError(5, "Access is denied")
        real_replace(a, b)

    monkeypatch.setattr(utils.os, "replace", flaky_replace)
    monkeypatch.setattr(utils.time, "sleep", lambda _s: None)  # skip real backoff wait

    utils.atomic_replace(src, dst)

    assert calls["n"] == 3
    assert dst.read_text(encoding="utf-8") == "payload"
    assert not src.exists()


def test_atomic_replace_reraises_after_exhausting_attempts(tmp_path, monkeypatch) -> None:
    from app.core import utils

    src = tmp_path / "s.tmp"
    dst = tmp_path / "d.json"
    src.write_text("x", encoding="utf-8")

    def always_denied(_a: Any, _b: Any) -> None:
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(utils.os, "replace", always_denied)
    monkeypatch.setattr(utils.time, "sleep", lambda _s: None)

    with pytest.raises(PermissionError):
        utils.atomic_replace(src, dst, attempts=3)


def test_atomic_replace_does_not_retry_other_errors(tmp_path, monkeypatch) -> None:
    from app.core import utils

    src = tmp_path / "s.tmp"
    dst = tmp_path / "d.json"

    calls = {"n": 0}

    def missing_src(_a: Any, _b: Any) -> None:
        calls["n"] += 1
        raise FileNotFoundError("no such file")

    monkeypatch.setattr(utils.os, "replace", missing_src)
    monkeypatch.setattr(utils.time, "sleep", lambda _s: None)

    with pytest.raises(FileNotFoundError):
        utils.atomic_replace(src, dst)
    assert calls["n"] == 1  # real fault surfaces immediately, no retry loop


def test_local_storage_detects_checksum_mismatch(tmp_path) -> None:
    settings = Settings(root_dir=tmp_path, backend_root=tmp_path, upload_part_size_mb=1)
    storage = LocalObjectStorageService(settings)
    asyncio.run(storage.ensure_layout())
    upload = {
        "id": "u1",
        "status": "initiated",
        "files": [
            {
                "fileId": "f1",
                "kind": "video",
                "key": "sessions/s1/source/video/video.mp4",
                "mimeType": "video/mp4",
                "sizeBytes": 3,
                "checksumSha256": "0" * 64,
                "parts": [],
                "status": "initiated",
            }
        ],
    }
    asyncio.run(storage.put_part(upload, "f1", 1, b"abc"))
    with pytest.raises(AppError):
        asyncio.run(storage.complete_file(upload, upload["files"][0]))
    assert not (settings.object_storage_root / "sessions/s1/source/video/video.mp4.assembling").exists()
