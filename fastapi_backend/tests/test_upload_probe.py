"""The early server check of a video while it uploads (GET /api/uploads/{id}/probe).

Real ffmpeg/ffprobe generate tiny fixtures, so what is asserted is what ffprobe
genuinely says about a *truncated* file, not what a fake would say.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.core.exceptions import AppError
from app.domain.enums import UploadProbeStatus, UploadProbeWarning, UploadStatus
from app.schemas.uploads import InitiateUploadRequest
from app.services.container import create_container
from tests.test_routes import build_test_client
from tests.test_session_maintenance import _init, _settings
from tests.test_user_admin import GOOD_PASSWORD, MARKER_EMAIL, _activate_marker, _headers, _token

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg/ffprobe not installed"
)


def _encoders() -> str:
    return subprocess.run(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True, text=True).stdout


def _ffmpeg(directory: Path, name: str, *args: str, audio: bool = True, video: str = "libx264") -> Path:
    out = directory / name
    inputs = ["-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=6"]
    if audio:
        inputs += ["-f", "lavfi", "-i", "sine=duration=6"]
    codec = ["-c:v", video, "-pix_fmt", "yuv420p"] + (["-c:a", "aac"] if audio else [])
    subprocess.run(["ffmpeg", "-v", "error", "-y", *inputs, *codec, *args, str(out)], check=True)
    return out


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory) -> dict[str, Path]:
    if shutil.which("ffmpeg") is None:
        return {}
    d = tmp_path_factory.mktemp("probe-fixtures")
    made = {
        "faststart": _ffmpeg(d, "fast.mp4", "-movflags", "+faststart"),
        "index_at_end": _ffmpeg(d, "slow.mp4"),
        "no_audio": _ffmpeg(d, "silent.mp4", "-movflags", "+faststart", audio=False),
        "two_audio": None,
        "mkv": _ffmpeg(d, "clip.mkv"),
    }
    two = d / "two.mkv"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=25:duration=3",
         "-f", "lavfi", "-i", "sine=duration=3", "-f", "lavfi", "-i", "sine=frequency=500:duration=3",
         "-map", "0", "-map", "1", "-map", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(two)],
        check=True,
    )
    made["two_audio"] = two
    rotated = d / "rot.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-display_rotation", "90", "-i", str(made["no_audio"]), "-c", "copy",
         "-movflags", "+faststart", str(rotated)],
        check=True,
    )
    made["rotated"] = rotated
    if "libx265" in _encoders():
        made["hevc"] = _ffmpeg(d, "hevc.mp4", "-movflags", "+faststart", video="libx265")
    return made


async def _start(container, name: str, size: int, *, actor=None) -> tuple[str, str]:
    initiated = await container.async_uploads.initiate(
        InitiateUploadRequest(
            files=[
                {"kind": "video", "originalName": name, "mimeType": "video/mp4", "sizeBytes": size},
                {"kind": "caseStudy", "originalName": "case.pdf", "mimeType": "application/pdf", "sizeBytes": 4},
            ],
            autoProcess=False,
        ),
        actor=actor,
    )
    upload = await container.async_uploads.repository.read(initiated["uploadId"])
    video = next(f for f in upload["files"] if f["kind"] == "video")
    return initiated["uploadId"], video["fileId"]


def _send(container, upload_id: str, file_id: str, data: bytes, numbers: list[int], chunk: int) -> None:
    for number in numbers:
        piece = data[(number - 1) * chunk : number * chunk]
        asyncio.run(container.async_uploads.put_part(upload_id, file_id, number, piece))


@pytest.fixture
def container(tmp_path):
    box = create_container(_settings(tmp_path))
    asyncio.run(_init(box))
    yield box
    asyncio.run(box.shutdown())


def _probe(container, upload_id: str) -> dict[str, Any]:
    return asyncio.run(container.async_uploads.probe_upload(upload_id))


# --- storage head -----------------------------------------------------------------


def _head(container, upload_id: str, file_id: str, limit: int) -> bytes | None:
    upload = asyncio.run(container.async_uploads.repository.read(upload_id))
    return asyncio.run(container.storage.read_received_head(upload, file_id, limit))


def test_head_is_none_until_part_one_arrives(container) -> None:
    upload_id, file_id = asyncio.run(_start(container, "a.mp4", 30))
    assert _head(container, upload_id, file_id, 100) is None
    _send(container, upload_id, file_id, b"x" * 30, [2], 10)  # part 2 only
    assert _head(container, upload_id, file_id, 100) is None


def test_head_stops_at_the_first_gap_and_is_capped(container) -> None:
    upload_id, file_id = asyncio.run(_start(container, "a.mp4", 50))
    data = bytes(range(50))
    _send(container, upload_id, file_id, data, [1, 2, 4], 10)  # gap at 3
    assert _head(container, upload_id, file_id, 100) == data[:20]
    assert _head(container, upload_id, file_id, 15) == data[:15]
    assert _head(container, upload_id, file_id, 0) is None


def test_head_ends_where_a_part_file_has_vanished(container) -> None:
    upload_id, file_id = asyncio.run(_start(container, "a.mp4", 30))
    _send(container, upload_id, file_id, b"y" * 30, [1, 2], 10)
    container.storage._part_path(upload_id, file_id, 2).unlink()
    assert _head(container, upload_id, file_id, 100) == b"y" * 10


def test_gcs_cannot_read_a_head() -> None:
    from app.storage.gcs import GcsObjectStorageService

    storage = GcsObjectStorageService.__new__(GcsObjectStorageService)
    assert asyncio.run(storage.read_received_head({}, "f", 10)) is None


# --- probe outcomes -----------------------------------------------------------------


def test_no_part_yet_is_pending(container) -> None:
    upload_id, _ = asyncio.run(_start(container, "a.mp4", 10))
    result = _probe(container, upload_id)
    assert result == {"status": UploadProbeStatus.PENDING, "receivedBytes": 0, "facts": None, "warnings": []}


@needs_ffmpeg
def test_faststart_head_gives_facts_and_no_warnings(container, fixtures) -> None:
    data = fixtures["faststart"].read_bytes()
    upload_id, file_id = asyncio.run(_start(container, "station.mp4", len(data)))
    _send(container, upload_id, file_id, data, [1], 20_000)  # a partial head
    result = _probe(container, upload_id)
    assert result["status"] == UploadProbeStatus.READY
    assert result["receivedBytes"] == 20_000
    assert result["warnings"] == []
    facts = result["facts"]
    assert (facts["width"], facts["height"]) == (320, 240)
    assert facts["videoCodec"] == "h264" and facts["audioCodec"] == "aac"
    assert facts["audioStreams"] == 1 and facts["rotation"] == 0
    assert facts["browserPlayable"] is True
    assert facts["durationSeconds"] == pytest.approx(6.0, abs=0.2)


@needs_ffmpeg
def test_mp4_with_its_index_at_the_end_is_inconclusive(container, fixtures) -> None:
    data = fixtures["index_at_end"].read_bytes()
    upload_id, file_id = asyncio.run(_start(container, "station.mp4", len(data)))
    _send(container, upload_id, file_id, data, [1], 20_000)
    result = _probe(container, upload_id)
    assert result["status"] == UploadProbeStatus.INCONCLUSIVE
    assert result["facts"] is None and result["warnings"] == []


@needs_ffmpeg
def test_head_without_audio_warns(container, fixtures) -> None:
    data = fixtures["no_audio"].read_bytes()
    upload_id, file_id = asyncio.run(_start(container, "silent.mp4", len(data)))
    _send(container, upload_id, file_id, data, [1], len(data))
    assert _probe(container, upload_id)["warnings"] == [UploadProbeWarning.NO_AUDIO_STREAM]


@needs_ffmpeg
def test_several_audio_streams_and_mkv_are_read(container, fixtures) -> None:
    data = fixtures["two_audio"].read_bytes()
    upload_id, file_id = asyncio.run(_start(container, "clip.mkv", len(data)))
    _send(container, upload_id, file_id, data, [1], len(data))
    result = _probe(container, upload_id)
    assert result["status"] == UploadProbeStatus.READY
    assert result["warnings"] == [UploadProbeWarning.MULTIPLE_AUDIO_STREAMS]
    assert result["facts"]["audioStreams"] == 2


@needs_ffmpeg
def test_rotation_is_reported(container, fixtures) -> None:
    data = fixtures["rotated"].read_bytes()
    upload_id, file_id = asyncio.run(_start(container, "phone.mp4", len(data)))
    _send(container, upload_id, file_id, data, [1], len(data))
    result = _probe(container, upload_id)
    assert result["facts"]["rotation"] == 90
    assert UploadProbeWarning.ROTATED in result["warnings"]


@needs_ffmpeg
def test_hevc_head_will_be_converted(container, fixtures) -> None:
    if "hevc" not in fixtures:
        pytest.skip("libx265 not installed")
    data = fixtures["hevc"].read_bytes()
    upload_id, file_id = asyncio.run(_start(container, "cam.mp4", len(data)))
    _send(container, upload_id, file_id, data, [1], len(data))
    result = _probe(container, upload_id)
    assert result["facts"]["browserPlayable"] is False
    assert UploadProbeWarning.WILL_CONVERT in result["warnings"]


@needs_ffmpeg
def test_a_file_with_no_video_stream_is_flagged(container, tmp_path) -> None:
    audio_only = tmp_path / "a.mkv"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=duration=2", str(audio_only)], check=True)
    data = audio_only.read_bytes()
    upload_id, file_id = asyncio.run(_start(container, "talk.mkv", len(data)))
    _send(container, upload_id, file_id, data, [1], len(data))
    assert UploadProbeWarning.NO_VIDEO_STREAM in _probe(container, upload_id)["warnings"]


def test_head_is_capped_before_it_reaches_ffprobe(container, monkeypatch) -> None:
    seen: list[int] = []

    async def fake_probe(path: Path, **_: Any) -> dict[str, Any]:
        seen.append(path.stat().st_size)
        return {"codec": "h264", "pixelFormat": "yuv420p", "audioCodec": "aac", "audioStreams": 1, "rotation": 0,
                "width": 2, "height": 2, "durationSeconds": 1.0}

    monkeypatch.setattr(container.media, "probe_video_stream", fake_probe)
    monkeypatch.setattr("app.services.async_upload_service.HEAD_MAX_BYTES", 25)
    upload_id, file_id = asyncio.run(_start(container, "a.mp4", 40))
    _send(container, upload_id, file_id, b"z" * 40, [1, 2, 3, 4], 10)
    assert _probe(container, upload_id)["status"] == UploadProbeStatus.READY
    assert seen == [25]


def test_failure_and_timeout_are_inconclusive_and_leave_no_temp_files(container, monkeypatch, tmp_path) -> None:
    paths: list[Path] = []

    async def failing(path: Path, **_: Any) -> dict[str, Any]:
        paths.append(path)
        raise RuntimeError("Video codec (ffprobe) timed out after 20s and was terminated.")

    monkeypatch.setattr(container.media, "probe_video_stream", failing)
    upload_id, file_id = asyncio.run(_start(container, "a.mp4", 10))
    _send(container, upload_id, file_id, b"z" * 10, [1], 10)
    assert _probe(container, upload_id)["status"] == UploadProbeStatus.INCONCLUSIVE
    assert paths and not paths[0].exists() and not paths[0].parent.exists()


def test_repeated_polls_probe_once_per_head_length(container, monkeypatch) -> None:
    calls: list[int] = []

    async def counting(path: Path, **_: Any) -> dict[str, Any]:
        calls.append(path.stat().st_size)
        return {"codec": "h264", "pixelFormat": "yuv420p", "audioStreams": 1, "audioCodec": "aac"}

    monkeypatch.setattr(container.media, "probe_video_stream", counting)
    upload_id, file_id = asyncio.run(_start(container, "a.mp4", 30))
    _send(container, upload_id, file_id, b"q" * 30, [1], 10)
    for _ in range(3):
        _probe(container, upload_id)
    assert calls == [10]
    _send(container, upload_id, file_id, b"q" * 30, [2], 10)  # a longer head is a new question
    _probe(container, upload_id)
    assert calls == [10, 20]


def test_cache_is_bounded(container, monkeypatch) -> None:
    async def fake(path: Path, **_: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(container.media, "probe_video_stream", fake)
    prober = container.async_uploads._head_prober
    prober._cache_entries = 3
    for length in range(1, 8):
        asyncio.run(prober.inspect("u", "f", "a.mp4", b"x" * length))
    assert len(prober._cache) == 3


def test_concurrent_probe_and_parts_keep_the_ledger_intact(container, monkeypatch) -> None:
    async def slow(path: Path, **_: Any) -> dict[str, Any]:
        await asyncio.sleep(0.2)
        return {}

    monkeypatch.setattr(container.media, "probe_video_stream", slow)

    async def run() -> None:
        upload_id, file_id = await _start(container, "a.mp4", 60)
        await container.async_uploads.put_part(upload_id, file_id, 1, b"a" * 10)
        probes = asyncio.gather(*(container.async_uploads.probe_upload(upload_id) for _ in range(3)))
        await asyncio.sleep(0.05)
        await asyncio.gather(*(container.async_uploads.put_part(upload_id, file_id, n, b"a" * 10) for n in range(2, 7)))
        await probes
        upload = await container.async_uploads.repository.read(upload_id)
        video = next(f for f in upload["files"] if f["kind"] == "video")
        assert [p["partNumber"] for p in video["parts"]] == [1, 2, 3, 4, 5, 6]

    asyncio.run(run())


def test_object_storage_without_relayed_parts_is_unavailable(container, monkeypatch) -> None:
    monkeypatch.setattr(container.storage, "relays_parts", False)
    upload_id, _ = asyncio.run(_start(container, "a.mp4", 10))
    assert _probe(container, upload_id)["status"] == UploadProbeStatus.UNAVAILABLE


def test_committed_upload_points_at_the_session_media(container) -> None:
    async def run() -> dict[str, Any]:
        upload_id, _ = await _start(container, "a.mp4", 10)
        upload = await container.async_uploads.repository.read(upload_id)
        upload["status"] = UploadStatus.COMMITTED
        await container.async_uploads.repository.write(upload)
        await container.sessions.update(
            upload["sessionId"],
            lambda s: s.setdefault("files", {}).__setitem__(
                "video", {"media": {"videoCodec": "h264", "audioCodec": "aac", "width": 4, "height": 2,
                                    "durationSeconds": 9.5, "browserPlayable": True}}),
        )
        return await container.async_uploads.probe_upload(upload_id)

    result = asyncio.run(run())
    assert result["status"] == UploadProbeStatus.COMPLETE
    assert result["facts"]["durationSeconds"] == 9.5 and result["warnings"] == []


def test_committed_upload_without_media_has_no_facts(container) -> None:
    async def run() -> dict[str, Any]:
        upload_id, _ = await _start(container, "a.mp4", 10)
        upload = await container.async_uploads.repository.read(upload_id)
        upload["status"] = UploadStatus.COMMITTED
        await container.async_uploads.repository.write(upload)
        return await container.async_uploads.probe_upload(upload_id)

    result = asyncio.run(run())
    assert result["status"] == UploadProbeStatus.COMPLETE and result["facts"] is None


def test_an_aborted_upload_is_refused(container) -> None:
    async def run() -> None:
        upload_id, _ = await _start(container, "a.mp4", 10)
        await container.async_uploads.abort(upload_id)
        with pytest.raises(AppError) as error:
            await container.async_uploads.probe_upload(upload_id)
        assert error.value.status_code == 409

    asyncio.run(run())


# --- the route ----------------------------------------------------------------------

UPLOAD = {
    "workflow": "standard",
    "autoProcess": False,
    "files": [
        {"kind": "video", "originalName": "station.mp4", "mimeType": "video/mp4", "sizeBytes": 5},
        {"kind": "caseStudy", "originalName": "case.pdf", "mimeType": "application/pdf", "sizeBytes": 4},
    ],
}


def test_route_owner_other_marker_and_unknown_id(tmp_path) -> None:
    client = build_test_client(tmp_path)
    admin = _token(client)
    _activate_marker(client, admin, MARKER_EMAIL)
    owner = _token(client, MARKER_EMAIL, GOOD_PASSWORD)
    _activate_marker(client, admin, "second@example.edu")
    other = _token(client, "second@example.edu", GOOD_PASSWORD)
    upload_id = client.post("/api/uploads/initiate", headers=_headers(owner), json=UPLOAD).json()["uploadId"]

    mine = client.get(f"/api/uploads/{upload_id}/probe", headers=_headers(owner))
    assert mine.status_code == 200, mine.text
    assert mine.json()["status"] == UploadProbeStatus.PENDING
    assert client.get(f"/api/uploads/{upload_id}/probe", headers=_headers(other)).status_code == 403
    assert client.get("/api/uploads/nope/probe", headers=_headers(owner)).status_code == 404
    assert client.get(f"/api/uploads/{upload_id}/probe").status_code == 401
