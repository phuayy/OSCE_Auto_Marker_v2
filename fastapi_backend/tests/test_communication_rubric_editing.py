"""Draft extraction, editing and saving of the communication rubric."""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import UploadFile

from app.core.config import Settings
from app.domain.communication_rubric import RubricValidationError, rubric_fingerprint
from app.services.artifact_service import ArtifactService
from app.services.rubric_service import DRAFT_TTL_SECONDS, RubricDraftNotFound, RubricParseError, RubricService
from tests.fixtures.rubric_versions import InMemoryRubricVersions
from tests.test_routes import build_test_client

REPO = Path(__file__).resolve().parents[2]

PARSED = {
    "schema": "communication-rubric-v1",
    "title": "Parsed",
    "criteria": [{"id": 1, "label": "A", "indicators": ["a"], "section": None}],
    "extraction": {"method": "llm", "model": "m-1", "provider": "p-1"},
}
EDIT = {
    "title": "Edited",
    "criteria": [
        {"label": "Opens well", "indicators": ["greets"], "section": None},
        {"label": "Closes well", "indicators": ["summarises"], "section": None},
    ],
}
ACTOR = SimpleNamespace(to_provenance=lambda: {"userId": "u1", "username": "dr", "displayName": "Dr R"})


class FakeRunner:
    def __init__(self, payload: dict[str, Any] | None = None, error: str | None = None) -> None:
        self.payload = payload or PARSED
        self.error = error
        self.calls: list[list[str]] = []

    async def run(self, command: str, args: list[str], label: str, *, env: dict[str, str] | None = None, **_: Any):
        self.calls.append(args)
        if self.error:
            raise RuntimeError(f"{label} failed with exit code 1.\nError: {self.error}")
        Path(args[args.index("--output") + 1]).write_text(json.dumps(self.payload), encoding="utf-8")


class FakeAssets:
    def __init__(self) -> None:
        self.n = 0

    async def register_communication_rubric(self, *, path: Path, original_name: str, mime_type: str | None):
        self.n += 1
        return SimpleNamespace(
            asset={"id": f"asset-{self.n}", "absolutePath": str(path), "contentSha256": f"sha{self.n}"},
            is_duplicate=False,
        )


def _service(tmp_path: Path, runner: FakeRunner) -> RubricService:
    root = tmp_path / "root"
    root.mkdir()
    (root / "scripts").symlink_to(REPO / "scripts")
    settings = Settings(root_dir=root, backend_root=root)
    settings.paths.ensure_layout()
    return RubricService(settings, runner, ArtifactService(settings), FakeAssets(), InMemoryRubricVersions())  # type: ignore[arg-type]


def _upload(tmp_path: Path, name: str = "new.pdf", ctype: str = "application/pdf") -> UploadFile:
    f = tmp_path / name
    f.write_bytes(b"%PDF-1.4 new")
    return UploadFile(file=f.open("rb"), filename=name, headers={"content-type": ctype})


def _seed_active(svc: RubricService) -> None:
    paths = svc.settings.paths
    paths.auth_dir.mkdir(parents=True, exist_ok=True)
    paths.communication_rubric_pdf_path.write_bytes(b"OLD PDF")
    paths.communication_rubric_json_path.write_text(
        json.dumps(
            {
                "title": "OLD",
                "criteria": [{"id": 1, "label": "Old", "indicators": [], "section": None}],
                "rubric_asset_id": "old-asset",
                "rubric_content_sha256": "oldsha",
                "source_pdf": "/x/old.pdf",
                "extraction": {"method": "llm", "model": "m-0", "provider": "p-0"},
            }
        ),
        encoding="utf-8",
    )


def _active(svc: RubricService) -> dict[str, Any]:
    return json.loads(svc.settings.paths.communication_rubric_json_path.read_text())


def _drafts(svc: RubricService) -> list[Path]:
    return list(svc.drafts_dir.glob("*")) if svc.drafts_dir.exists() else []


# --- drafts -----------------------------------------------------------------


def test_extract_draft_llm_mode_passes_force_llm_and_auto_does_not(tmp_path: Path) -> None:
    runner = FakeRunner()
    svc = _service(tmp_path, runner)
    asyncio.run(svc.extract_draft(_upload(tmp_path), mode="llm"))
    asyncio.run(svc.extract_draft(_upload(tmp_path), mode="auto"))
    assert "--force-llm" in runner.calls[0]
    assert "--force-llm" not in runner.calls[1]
    with pytest.raises(ValueError):
        asyncio.run(svc.extract_draft(_upload(tmp_path), mode="bogus"))


def test_extract_draft_never_touches_active_rubric(tmp_path: Path) -> None:
    svc = _service(tmp_path, FakeRunner())
    _seed_active(svc)
    before = _active(svc)
    result = asyncio.run(svc.extract_draft(_upload(tmp_path), actor=ACTOR))
    assert svc.settings.paths.communication_rubric_pdf_path.read_bytes() == b"OLD PDF"
    assert _active(svc) == before
    draft_id = result["draft"]["draftId"]
    assert result["rubric"]["title"] == "Parsed" and result["draft"]["originalName"] == "new.pdf"
    doc = json.loads((svc.drafts_dir / draft_id / "draft.json").read_text())
    assert doc["createdBy"]["username"] == "dr" and doc["rubric"]["title"] == "Parsed"
    assert (svc.drafts_dir / draft_id / "source.pdf").read_bytes() == b"%PDF-1.4 new"


def test_extract_draft_parse_failure_removes_draft(tmp_path: Path) -> None:
    svc = _service(tmp_path, FakeRunner(error="AI extraction failed: nope"))
    with pytest.raises(RubricParseError, match="AI extraction failed"):
        asyncio.run(svc.extract_draft(_upload(tmp_path)))
    assert _drafts(svc) == []


def test_extract_draft_rejects_non_pdf(tmp_path: Path) -> None:
    svc = _service(tmp_path, FakeRunner())
    with pytest.raises(ValueError):
        asyncio.run(svc.extract_draft(_upload(tmp_path, "a.txt", "text/plain")))
    assert _drafts(svc) == []


@pytest.mark.parametrize("bad", ["../x", "abc", "A" * 32, "0" * 31, "0" * 33, "../" + "0" * 29, ""])
def test_invalid_draft_ids_are_not_found_without_touching_fs(tmp_path: Path, bad: str) -> None:
    svc = _service(tmp_path, FakeRunner())
    outside = svc.settings.paths.auth_dir / "x"
    outside.mkdir(parents=True, exist_ok=True)
    with pytest.raises(RubricDraftNotFound):
        asyncio.run(svc.discard_draft(bad))
    with pytest.raises(RubricDraftNotFound):
        asyncio.run(svc.save_edited(EDIT, draft_id=bad))
    assert outside.is_dir()
    assert not svc.drafts_dir.exists()


def test_discard_deletes_then_404(tmp_path: Path) -> None:
    svc = _service(tmp_path, FakeRunner())
    draft_id = asyncio.run(svc.extract_draft(_upload(tmp_path)))["draft"]["draftId"]
    asyncio.run(svc.discard_draft(draft_id))
    assert _drafts(svc) == []
    with pytest.raises(RubricDraftNotFound):
        asyncio.run(svc.discard_draft(draft_id))


def _age(svc: RubricService, draft_id: str) -> None:
    old = time.time() - DRAFT_TTL_SECONDS - 60
    d = svc.drafts_dir / draft_id
    for p in (d / "draft.json", d):
        os.utime(p, (old, old))


def test_expired_drafts_swept_on_next_extract(tmp_path: Path) -> None:
    svc = _service(tmp_path, FakeRunner())
    old_id = asyncio.run(svc.extract_draft(_upload(tmp_path)))["draft"]["draftId"]
    _age(svc, old_id)
    new_id = asyncio.run(svc.extract_draft(_upload(tmp_path)))["draft"]["draftId"]
    assert [p.name for p in _drafts(svc)] == [new_id]


def test_saving_expired_draft_is_not_found(tmp_path: Path) -> None:
    svc = _service(tmp_path, FakeRunner())
    draft_id = asyncio.run(svc.extract_draft(_upload(tmp_path)))["draft"]["draftId"]
    _age(svc, draft_id)
    with pytest.raises(RubricDraftNotFound):
        asyncio.run(svc.save_edited(EDIT, draft_id=draft_id))


# --- save_edited ------------------------------------------------------------


def test_save_edited_with_draft_activates_pdf_and_json(tmp_path: Path) -> None:
    svc = _service(tmp_path, FakeRunner())
    _seed_active(svc)
    draft_id = asyncio.run(svc.extract_draft(_upload(tmp_path)))["draft"]["draftId"]
    result = asyncio.run(svc.save_edited(EDIT, draft_id=draft_id, actor=ACTOR))
    stored = _active(svc)
    assert svc.settings.paths.communication_rubric_pdf_path.read_bytes() == b"%PDF-1.4 new"
    assert stored["title"] == "Edited" and len(stored["criteria"]) == 2
    ext = stored["extraction"]
    assert ext["method"] == "edited" and ext["source"]["method"] == "llm" and ext["source"]["model"] == "m-1"
    assert ext["edited_by"]["username"] == "dr"
    assert stored["rubric_revision"] == rubric_fingerprint(stored)
    assert stored["rubric_asset_id"].startswith("asset-")
    assert Path(stored["source_pdf"]).read_bytes() == b"%PDF-1.4 new"  # survives draft removal
    assert _drafts(svc) == []
    assert result["rubric"] == stored and "pdf" in result and "defaultSourcePdf" in result


def test_save_edited_in_place_keeps_asset_and_source(tmp_path: Path) -> None:
    svc = _service(tmp_path, FakeRunner())
    _seed_active(svc)
    result = asyncio.run(svc.save_edited(EDIT, actor=ACTOR))
    stored = _active(svc)
    assert stored["rubric_asset_id"] == "old-asset" and stored["source_pdf"] == "/x/old.pdf"
    assert stored["rubric_content_sha256"] == "oldsha"
    assert stored["extraction"]["source"]["model"] == "m-0"
    assert stored["rubric_revision"] == rubric_fingerprint(stored)
    assert svc.settings.paths.communication_rubric_pdf_path.read_bytes() == b"OLD PDF"
    assert result["rubric"]["title"] == "Edited"


def test_validation_error_leaves_active_unchanged(tmp_path: Path) -> None:
    svc = _service(tmp_path, FakeRunner())
    _seed_active(svc)
    before = _active(svc)
    with pytest.raises(RubricValidationError) as err:
        asyncio.run(svc.save_edited({"title": "", "criteria": []}))
    assert err.value.issues
    assert _active(svc) == before


def test_in_place_save_without_active_rubric(tmp_path: Path) -> None:
    svc = _service(tmp_path, FakeRunner())
    with pytest.raises(RubricParseError, match="no active"):
        asyncio.run(svc.save_edited(EDIT))


def test_upload_and_reset_stamp_revision(tmp_path: Path) -> None:
    svc = _service(tmp_path, FakeRunner())
    asyncio.run(svc.upload(_upload(tmp_path)))
    stored = _active(svc)
    assert stored["rubric_revision"] == rubric_fingerprint(stored)
    if svc.settings.paths.default_rubric_source_pdf.exists():
        asyncio.run(svc.reset())
        stored = _active(svc)
        assert stored["rubric_revision"] == rubric_fingerprint(stored)


def test_concurrent_saves_do_not_interleave(tmp_path: Path) -> None:
    svc = _service(tmp_path, FakeRunner())
    _seed_active(svc)
    a = {"title": "A", "criteria": [{"label": "a1", "indicators": ["x"]}, {"label": "a2", "indicators": ["y"]}]}
    b = {"title": "B", "criteria": [{"label": "b1", "indicators": ["x"]}, {"label": "b2", "indicators": ["y"]}, {"label": "b3"}]}

    async def both() -> None:
        await asyncio.gather(svc.save_edited(a), svc.save_edited(b))

    asyncio.run(both())
    stored = _active(svc)
    labels = [c["label"] for c in stored["criteria"]]
    assert (stored["title"], labels) in [("A", ["a1", "a2"]), ("B", ["b1", "b2", "b3"])]
    assert stored["rubric_revision"] == rubric_fingerprint(stored)


# --- routes -----------------------------------------------------------------


def _client(tmp_path: Path, runner: FakeRunner | None = None):
    (tmp_path / "scripts").symlink_to(REPO / "scripts")
    client = build_test_client(tmp_path)
    container = client.app.state.container
    container.rubrics.runner = runner or FakeRunner()
    return client, container.rubrics


def _login(client, username="admin", password="admin") -> dict[str, str]:
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


def _marker_headers(client, admin: dict[str, str]) -> dict[str, str]:
    r = client.post("/api/admin/users", json={"email": "m@example.edu", "role": "marker"}, headers=admin)
    assert r.status_code == 201, r.text
    token = client.app.state.container.mailer.last_token()
    ok = client.post(f"/api/auth/invitations/{token}/accept", json={"password": "correct-horse-battery"})
    assert ok.status_code == 200, ok.text
    return _login(client, "m@example.edu", "correct-horse-battery")


def _post_draft(client, headers, mode: str | None = None, name="r.pdf", ctype="application/pdf"):
    url = "/api/admin/communication-rubric/drafts" + (f"?mode={mode}" if mode else "")
    return client.post(url, files={"rubric": (name, b"%PDF-1.4 x", ctype)}, headers=headers)


def test_route_draft_save_flow(tmp_path: Path) -> None:
    runner = FakeRunner()
    client, svc = _client(tmp_path, runner)
    admin = _login(client)
    r = _post_draft(client, admin)
    assert r.status_code == 201, r.text
    assert "--force-llm" in runner.calls[0]
    draft_id = r.json()["draft"]["draftId"]
    saved = client.put("/api/admin/communication-rubric", json={"rubric": EDIT, "draftId": draft_id}, headers=admin)
    assert saved.status_code == 200, saved.text
    assert saved.json()["rubric"]["extraction"]["edited_by"]["username"] == "admin"
    assert client.get("/api/communication-rubric", headers=admin).json()["rubric"]["title"] == "Edited"
    again = client.put("/api/admin/communication-rubric", json={"rubric": EDIT, "draftId": draft_id}, headers=admin)
    assert again.status_code == 404 and "expired" in again.json()["detail"]


def test_route_auto_mode_and_bad_mode(tmp_path: Path) -> None:
    runner = FakeRunner()
    client, _ = _client(tmp_path, runner)
    admin = _login(client)
    assert _post_draft(client, admin, "auto").status_code == 201
    assert "--force-llm" not in runner.calls[0]
    assert _post_draft(client, admin, "weird").status_code == 400


def test_route_parse_failure_422_and_non_pdf_400(tmp_path: Path) -> None:
    client, svc = _client(tmp_path, FakeRunner(error="AI extraction is not available: off"))
    admin = _login(client)
    r = _post_draft(client, admin)
    assert r.status_code == 422 and "not available" in r.json()["detail"]
    assert not svc.drafts_dir.exists() or list(svc.drafts_dir.glob("*")) == []
    assert _post_draft(client, admin, name="a.txt", ctype="text/plain").status_code == 400
    missing = client.post("/api/admin/communication-rubric/drafts", headers=admin)
    assert missing.status_code == 400


def test_route_delete_draft(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    admin = _login(client)
    draft_id = _post_draft(client, admin).json()["draft"]["draftId"]
    url = f"/api/admin/communication-rubric/drafts/{draft_id}"
    assert client.delete(url, headers=admin).status_code == 204
    assert client.delete(url, headers=admin).status_code == 404
    assert client.delete("/api/admin/communication-rubric/drafts/..x", headers=admin).status_code == 404


def test_route_validation_error_lists_issues_and_keeps_active(tmp_path: Path) -> None:
    client, svc = _client(tmp_path)
    admin = _login(client)
    _seed_active(svc)
    before = _active(svc)
    r = client.put("/api/admin/communication-rubric", json={"rubric": {"title": "", "criteria": []}}, headers=admin)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["message"].startswith("The rubric has problems") and detail["issues"]
    assert _active(svc) == before


def test_route_in_place_save_without_rubric_is_422(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    admin = _login(client)
    r = client.put("/api/admin/communication-rubric", json={"rubric": EDIT}, headers=admin)
    assert r.status_code == 422 and "no active" in r.json()["detail"]


def test_routes_forbidden_for_marker_and_unauthenticated(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    marker = _marker_headers(client, _login(client))
    did = "0" * 32
    calls = [
        lambda h: client.post(
            "/api/admin/communication-rubric/drafts", files={"rubric": ("r.pdf", b"%PDF", "application/pdf")}, headers=h
        ),
        lambda h: client.delete(f"/api/admin/communication-rubric/drafts/{did}", headers=h),
        lambda h: client.put("/api/admin/communication-rubric", json={"rubric": EDIT}, headers=h),
    ]
    for call in calls:
        assert call(marker).status_code == 403
        assert call({}).status_code == 401
