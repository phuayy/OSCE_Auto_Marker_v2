"""Version history of the communication rubric: the table, the repository, the
service paths that create versions, restore, reconciliation of the scorer's
file with the database, and the HTTP routes.

Everything here runs against a real SQLite database through the real
repositories; only the parser subprocess is replaced (``ScriptedRunner``).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import UploadFile
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from app.core.config import Settings
from app.database.models import CommunicationRubricVersionRecord, utc_now
from app.database.orm import OrmDatabase
from app.domain.communication_rubric import RubricChangeKind, RubricVersionStatus, rubric_fingerprint
from app.repositories.communication_rubric_version_repository import CommunicationRubricVersionRepository
from app.repositories.rubric_asset_repository import RubricAssetRepository
from app.services.artifact_service import ArtifactService
from app.services.rubric_asset_service import RubricAssetService
from app.services.rubric_service import RubricService, RubricVersionAlreadyActive, RubricVersionNotFound
from tests.test_communication_rubric_editing import EDIT, _client, _login, _marker_headers

REPO = Path(__file__).resolve().parents[2]
ACTOR = SimpleNamespace(to_provenance=lambda: {"userId": "u1", "username": "dr", "displayName": "Dr R"})


def _rubric(title: str, *, pass_threshold: int | None = None, labels: tuple[str, ...] = ("A", "B")) -> dict[str, Any]:
    criteria = [{"id": i + 1, "label": label, "indicators": [label.lower()], "section": None} for i, label in enumerate(labels)]
    document: dict[str, Any] = {
        "schema": "communication-rubric-v1",
        "title": title,
        "criteria": criteria,
        "criteria_count": len(criteria),
        "max_score": 3 * len(criteria),
        "scoring_scale": {"All": 3, "Most": 2, "Some": 1, "None": 0},
        "extraction": {"method": "deterministic"},
    }
    if pass_threshold is not None:
        document["pass_threshold"] = pass_threshold
    return document


class ScriptedRunner:
    """Stands in for the parser subprocess: writes ``self.payload`` to ``--output``."""

    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.payload = payload or _rubric("Parsed")
        self.calls = 0

    async def run(self, command: str, args: list[str], label: str, *, env: dict[str, str] | None = None, **_: Any):
        self.calls += 1
        Path(args[args.index("--output") + 1]).write_text(json.dumps(self.payload), encoding="utf-8")


def _settings(tmp_path: Path) -> Settings:
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    if not (root / "scripts").exists():
        (root / "scripts").symlink_to(REPO / "scripts")
    settings = Settings(root_dir=root, backend_root=root)
    settings.paths.ensure_layout()
    return settings


def _service(tmp_path: Path, runner: ScriptedRunner | None = None) -> tuple[RubricService, OrmDatabase]:
    settings = _settings(tmp_path)
    database = OrmDatabase(tmp_path / "app.sqlite3")
    service = RubricService(
        settings,
        runner or ScriptedRunner(),  # type: ignore[arg-type]
        ArtifactService(settings),
        RubricAssetService(RubricAssetRepository(database)),
        CommunicationRubricVersionRepository(database),
    )
    return service, database


def _upload(tmp_path: Path, content: bytes, name: str = "rubric.pdf") -> UploadFile:
    path = tmp_path / f"upload-{abs(hash(content))}.pdf"
    path.write_bytes(content)
    return UploadFile(file=path.open("rb"), filename=name, headers={"content-type": "application/pdf"})


def _file(service: RubricService) -> dict[str, Any]:
    return json.loads(service.settings.paths.communication_rubric_json_path.read_text(encoding="utf-8"))


def _run(coro_factory):
    """One event loop per test: the async engine's connections belong to it."""

    async def main():
        return await coro_factory()

    return asyncio.run(main())


# --- schema -----------------------------------------------------------------


def test_migration_creates_the_table_and_downgrade_removes_it(tmp_path: Path) -> None:
    from alembic import command

    from app.database.migration_runner import _build_config, to_sync_url

    url = to_sync_url(OrmDatabase._normalize_url(tmp_path / "m.sqlite3"))
    command.upgrade(_build_config(url), "head")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            assert inspector.has_table("communication_rubric_versions")
            columns = {c["name"] for c in inspector.get_columns("communication_rubric_versions")}
            assert {"version_number", "status", "change_kind", "document_json", "rubric_asset_id"} <= columns
            indexes = {i["name"]: i for i in inspector.get_indexes("communication_rubric_versions")}
            assert indexes["uq_communication_rubric_versions_one_active"]["unique"]
            fks = inspector.get_foreign_keys("communication_rubric_versions")
            assert fks[0]["referred_table"] == "rubric_assets"
        command.downgrade(_build_config(url), "0016")
        with engine.connect() as connection:
            assert not inspect(connection).has_table("communication_rubric_versions")
    finally:
        engine.dispose()


def test_database_refuses_a_second_active_version(tmp_path: Path) -> None:
    async def scenario():
        database = OrmDatabase(tmp_path / "app.sqlite3")
        repo = CommunicationRubricVersionRepository(database)
        await repo.activate(_rubric("one"), change_kind=RubricChangeKind.UPLOAD, rubric_asset_id=None)
        with pytest.raises(IntegrityError):
            async with database.unit_of_work() as db:
                db.add(
                    CommunicationRubricVersionRecord(
                        id="rogue",
                        version_number=99,
                        status=RubricVersionStatus.ACTIVE,
                        change_kind=RubricChangeKind.UPLOAD,
                        rubric_revision="x",
                        document_json={},
                        created_at=utc_now(),
                    )
                )
                await db.flush()
        await database.shutdown()

    _run(scenario)


# --- repository -------------------------------------------------------------


def test_activate_numbers_versions_and_retires_the_previous_one(tmp_path: Path) -> None:
    async def scenario():
        database = OrmDatabase(tmp_path / "app.sqlite3")
        repo = CommunicationRubricVersionRepository(database)
        assert await repo.active() is None and await repo.list_versions() == []

        first = await repo.activate(
            {**_rubric("one"), "rubric_version": {"id": "stale", "number": 7}},
            change_kind=RubricChangeKind.UPLOAD,
            rubric_asset_id=None,
            created_by={"userId": "u1", "username": "dr", "displayName": "Dr R"},
        )
        second = await repo.activate(_rubric("two"), change_kind=RubricChangeKind.EDIT, rubric_asset_id=None)
        third = await repo.activate(
            _rubric("one"), change_kind=RubricChangeKind.RESTORE, rubric_asset_id=None, restored_from_version=1
        )

        assert [first["versionNumber"], second["versionNumber"], third["versionNumber"]] == [1, 2, 3]
        # The materialised marker is never stored: the row is the authority on its own id.
        assert "rubric_version" not in first["rubric"]
        assert first["rubricRevision"] == rubric_fingerprint(_rubric("one"))

        listed = await repo.list_versions()
        assert [(v["versionNumber"], v["status"]) for v in listed] == [(3, "active"), (2, "retired"), (1, "retired")]
        assert all("rubric" not in v for v in listed)
        assert listed[0]["restoredFromVersion"] == 1 and listed[0]["changeKind"] == "restore"
        assert listed[2]["createdBy"]["username"] == "dr"
        assert listed[0]["retiredAt"] is None and listed[1]["retiredAt"] and listed[2]["retiredAt"]
        assert (listed[2]["title"], listed[2]["criteriaCount"], listed[2]["maxScore"]) == ("one", 2, 6)
        assert [v["versionNumber"] for v in await repo.list_versions(limit=2)] == [3, 2]
        assert (await repo.active())["id"] == third["id"]
        assert (await repo.get(first["id"]))["rubric"]["title"] == "one"
        assert await repo.get("missing") is None
        await database.shutdown()

    _run(scenario)


def test_concurrent_activations_never_share_a_number_or_an_active_slot(tmp_path: Path) -> None:
    async def scenario():
        database = OrmDatabase(tmp_path / "app.sqlite3")
        repo = CommunicationRubricVersionRepository(database)
        await asyncio.gather(
            *(
                repo.activate(_rubric(f"r{i}"), change_kind=RubricChangeKind.UPLOAD, rubric_asset_id=None)
                for i in range(6)
            )
        )
        listed = await repo.list_versions()
        assert sorted(v["versionNumber"] for v in listed) == [1, 2, 3, 4, 5, 6]
        assert [v["status"] for v in listed].count("active") == 1
        await database.shutdown()

    _run(scenario)


# --- service: every change is a version --------------------------------------


def test_first_read_records_the_default_pdf_as_version_one(tmp_path: Path) -> None:
    async def scenario():
        service, database = _service(tmp_path)
        default_pdf = service.settings.paths.default_rubric_source_pdf
        default_pdf.parent.mkdir(parents=True, exist_ok=True)
        default_pdf.write_bytes(b"%PDF-1.4 shipped")

        current = await service.current()
        assert current["version"]["versionNumber"] == 1 and current["version"]["changeKind"] == "default"
        assert current["rubric"]["rubric_version"] == {"id": current["version"]["id"], "number": 1}
        assert _file(service) == current["rubric"]
        assert current["version"]["rubricAssetId"] == current["rubric"]["rubric_asset_id"]
        # A second read changes nothing.
        await service.current()
        assert len(await service.list_versions()) == 1
        await database.shutdown()

    _run(scenario)


def test_an_existing_rubric_file_is_adopted_unchanged(tmp_path: Path) -> None:
    async def scenario():
        runner = ScriptedRunner()
        service, database = _service(tmp_path, runner)
        legacy = {**_rubric("Legacy", pass_threshold=4), "rubric_asset_id": "gone-asset"}
        service.settings.paths.communication_rubric_json_path.write_text(json.dumps(legacy), encoding="utf-8")

        adopted = await service.ensure_parsed()
        versions = await service.list_versions()
        assert [(v["versionNumber"], v["changeKind"]) for v in versions] == [(1, "adopted")]
        assert adopted["title"] == "Legacy" and adopted["pass_threshold"] == 4
        # An asset id with no row is kept in the document but not linked.
        assert versions[0]["rubricAssetId"] is None and adopted["rubric_asset_id"] == "gone-asset"
        assert runner.calls == 0, "adoption must not re-parse anything"
        await database.shutdown()

    _run(scenario)


def test_upload_reset_draft_and_edit_each_retire_the_active_version(tmp_path: Path) -> None:
    async def scenario():
        runner = ScriptedRunner(_rubric("Uploaded"))
        service, database = _service(tmp_path, runner)
        default_pdf = service.settings.paths.default_rubric_source_pdf
        default_pdf.parent.mkdir(parents=True, exist_ok=True)
        default_pdf.write_bytes(b"%PDF-1.4 shipped")

        uploaded = await service.upload(_upload(tmp_path, b"%PDF-1.4 first"), actor=ACTOR)
        assert uploaded["version"]["versionNumber"] == 1 and uploaded["version"]["changeKind"] == "upload"
        assert uploaded["version"]["createdBy"]["username"] == "dr"

        runner.payload = _rubric("Second upload")
        await service.upload(_upload(tmp_path, b"%PDF-1.4 second"), actor=ACTOR)

        runner.payload = _rubric("Shipped")
        reset = await service.reset(actor=ACTOR)
        assert reset["version"]["changeKind"] == "reset" and reset["rubric"]["title"] == "Shipped"

        runner.payload = _rubric("From draft")
        draft = await service.extract_draft(_upload(tmp_path, b"%PDF-1.4 draft"), mode="llm", actor=ACTOR)
        assert len(await service.list_versions()) == 3, "an extraction alone is not a version"
        await service.save_edited(EDIT, draft_id=draft["draft"]["draftId"], actor=ACTOR)
        edited = await service.save_edited({**EDIT, "title": "Edited again"}, actor=ACTOR)

        versions = await service.list_versions()
        assert [(v["versionNumber"], v["changeKind"], v["status"]) for v in versions] == [
            (5, "edit", "active"),
            (4, "draft", "retired"),
            (3, "reset", "retired"),
            (2, "upload", "retired"),
            (1, "upload", "retired"),
        ]
        assert edited["version"]["id"] == versions[0]["id"]
        assert _file(service)["rubric_version"]["number"] == 5
        # An in-place edit keeps the PDF the draft brought in.
        assert versions[0]["rubricAssetId"] == versions[1]["rubricAssetId"] is not None
        await database.shutdown()

    _run(scenario)


def test_a_failed_parse_creates_no_version(tmp_path: Path) -> None:
    class FailingRunner(ScriptedRunner):
        async def run(self, *args: Any, **kwargs: Any):
            raise RuntimeError("Communication rubric parser failed.\nError: unreadable layout")

    async def scenario():
        service, database = _service(tmp_path, ScriptedRunner())
        await service.upload(_upload(tmp_path, b"%PDF-1.4 good"), actor=ACTOR)
        service.runner = FailingRunner()
        with pytest.raises(Exception, match="unreadable layout"):
            await service.upload(_upload(tmp_path, b"%PDF-1.4 bad"), actor=ACTOR)
        assert [v["versionNumber"] for v in await service.list_versions()] == [1]
        await database.shutdown()

    _run(scenario)


# --- restore ----------------------------------------------------------------


def test_restore_records_a_new_version_and_brings_back_rubric_and_pdf(tmp_path: Path) -> None:
    async def scenario():
        runner = ScriptedRunner(_rubric("First", pass_threshold=3, labels=("Open", "Close")))
        service, database = _service(tmp_path, runner)
        active_pdf = service.settings.paths.communication_rubric_pdf_path
        await service.upload(_upload(tmp_path, b"%PDF-1.4 first"), actor=ACTOR)
        first_file = _file(service)
        runner.payload = _rubric("Second", pass_threshold=5, labels=("X", "Y", "Z"))
        await service.upload(_upload(tmp_path, b"%PDF-1.4 second"), actor=ACTOR)
        assert active_pdf.read_bytes() == b"%PDF-1.4 second"

        v1 = (await service.list_versions())[-1]
        restored = await service.restore_version(v1["id"], actor=ACTOR)

        assert restored["version"]["versionNumber"] == 3
        assert restored["version"]["changeKind"] == "restore" and restored["version"]["restoredFromVersion"] == 1
        assert restored["version"]["rubricAssetId"] == v1["rubricAssetId"]
        on_disk = _file(service)
        assert on_disk["title"] == "First" and on_disk["pass_threshold"] == 3
        assert on_disk["rubric_revision"] == first_file["rubric_revision"]
        assert on_disk["extraction"] == first_file["extraction"], "a restore keeps the version's own provenance"
        assert on_disk["rubric_version"]["number"] == 3
        assert active_pdf.read_bytes() == b"%PDF-1.4 first"
        # The restored-from version itself is untouched and stays retired.
        assert (await service.get_version(v1["id"]))["status"] == "retired"
        await database.shutdown()

    _run(scenario)


def test_restoring_the_default_version_shows_the_shipped_pdf_again(tmp_path: Path) -> None:
    async def scenario():
        service, database = _service(tmp_path, ScriptedRunner(_rubric("Shipped")))
        default_pdf = service.settings.paths.default_rubric_source_pdf
        default_pdf.parent.mkdir(parents=True, exist_ok=True)
        default_pdf.write_bytes(b"%PDF-1.4 shipped")
        await service.ensure_parsed()
        await service.upload(_upload(tmp_path, b"%PDF-1.4 custom"), actor=ACTOR)
        assert (await service.read_pdf_meta())["isCustomUpload"] is True

        default_version = (await service.list_versions())[-1]
        await service.restore_version(default_version["id"], actor=ACTOR)
        meta = await service.read_pdf_meta()
        assert meta["isCustomUpload"] is False and Path(meta["absolutePath"]) == default_pdf
        await database.shutdown()

    _run(scenario)


def test_restore_refuses_the_active_version_and_unknown_ids(tmp_path: Path) -> None:
    async def scenario():
        service, database = _service(tmp_path)
        await service.upload(_upload(tmp_path, b"%PDF-1.4 only"), actor=ACTOR)
        active = (await service.list_versions())[0]
        with pytest.raises(RubricVersionAlreadyActive):
            await service.restore_version(active["id"], actor=ACTOR)
        with pytest.raises(RubricVersionNotFound):
            await service.restore_version("no-such-version", actor=ACTOR)
        with pytest.raises(RubricVersionNotFound):
            await service.get_version("no-such-version")
        assert len(await service.list_versions()) == 1
        await database.shutdown()

    _run(scenario)


def test_restore_still_restores_the_rubric_when_its_pdf_is_gone(tmp_path: Path) -> None:
    async def scenario():
        runner = ScriptedRunner(_rubric("First"))
        service, database = _service(tmp_path, runner)
        await service.upload(_upload(tmp_path, b"%PDF-1.4 first"), actor=ACTOR)
        v1 = (await service.list_versions())[0]
        Path((await service.rubric_assets.repository.read(v1["rubricAssetId"]))["absolutePath"]).unlink()
        runner.payload = _rubric("Second")
        await service.upload(_upload(tmp_path, b"%PDF-1.4 second"), actor=ACTOR)

        await service.restore_version(v1["id"], actor=ACTOR)
        assert _file(service)["title"] == "First"
        assert service.settings.paths.communication_rubric_pdf_path.read_bytes() == b"%PDF-1.4 second"
        await database.shutdown()

    _run(scenario)


# --- the scorer's file follows the database -----------------------------------


def test_a_missing_or_stale_file_is_rewritten_from_the_active_version(tmp_path: Path) -> None:
    async def scenario():
        service, database = _service(tmp_path, ScriptedRunner(_rubric("Active")))
        await service.upload(_upload(tmp_path, b"%PDF-1.4 a"), actor=ACTOR)
        expected = _file(service)
        path = service.settings.paths.communication_rubric_json_path

        path.unlink()
        assert await service.ensure_parsed() == expected and _file(service) == expected

        # A crash between the database commit and the file write leaves the
        # file naming the previous version; the next scoring run repairs it.
        path.write_text(json.dumps({**_rubric("Old"), "rubric_version": {"id": "old", "number": 0}}), encoding="utf-8")
        assert (await service.ensure_parsed())["title"] == "Active"
        assert _file(service) == expected
        assert len(await service.list_versions()) == 1, "repairing the file is not a new version"
        await database.shutdown()

    _run(scenario)


def test_scoring_falls_back_to_the_file_when_the_history_cannot_be_read(tmp_path: Path) -> None:
    async def scenario():
        service, database = _service(tmp_path, ScriptedRunner(_rubric("Active")))
        await service.upload(_upload(tmp_path, b"%PDF-1.4 a"), actor=ACTOR)
        expected = _file(service)

        class Broken:
            async def active(self):
                raise RuntimeError("database is down")

        service.versions = Broken()  # type: ignore[assignment]
        assert await service.ensure_parsed() == expected
        await database.shutdown()

    _run(scenario)


# --- routes -----------------------------------------------------------------


def _seed_two_versions(client, admin: dict[str, str]) -> list[dict[str, Any]]:
    client.app.state.container.rubrics.runner = ScriptedRunner(_rubric("First", pass_threshold=3))
    for name, body in (("a.pdf", b"%PDF-1.4 a"), ("b.pdf", b"%PDF-1.4 b")):
        r = client.post(
            "/api/admin/communication-rubric", files={"rubric": (name, body, "application/pdf")}, headers=admin
        )
        assert r.status_code == 200, r.text
        client.app.state.container.rubrics.runner = ScriptedRunner(_rubric("Second", pass_threshold=5))
    r = client.get("/api/communication-rubric/versions", headers=admin)
    assert r.status_code == 200
    return r.json()["versions"]


def test_routes_list_view_and_restore(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    admin = _login(client)
    versions = _seed_two_versions(client, admin)
    assert [(v["versionNumber"], v["status"]) for v in versions] == [(2, "active"), (1, "retired")]
    assert versions[0]["createdBy"]["username"] == "admin"

    current = client.get("/api/communication-rubric", headers=admin).json()
    assert current["version"]["versionNumber"] == 2 and "rubric" not in current["version"]

    one = client.get(f"/api/communication-rubric/versions/{versions[1]['id']}", headers=admin)
    assert one.status_code == 200 and one.json()["version"]["rubric"]["title"] == "First"
    assert client.get("/api/communication-rubric/versions/nope", headers=admin).status_code == 404
    assert client.get("/api/communication-rubric/versions?limit=1", headers=admin).json()["versions"][0][
        "versionNumber"
    ] == 2
    assert client.get("/api/communication-rubric/versions?limit=0", headers=admin).status_code == 400

    restored = client.post(f"/api/admin/communication-rubric/versions/{versions[1]['id']}/restore", headers=admin)
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["rubric"]["title"] == "First" and body["version"]["versionNumber"] == 3
    assert body["version"]["restoredFromVersion"] == 1

    again = client.post(f"/api/admin/communication-rubric/versions/{body['version']['id']}/restore", headers=admin)
    assert again.status_code == 409
    missing = client.post("/api/admin/communication-rubric/versions/nope/restore", headers=admin)
    assert missing.status_code == 404


def test_markers_read_the_history_but_cannot_restore(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    admin = _login(client)
    versions = _seed_two_versions(client, admin)
    marker = _marker_headers(client, admin)

    assert client.get("/api/communication-rubric/versions", headers=marker).status_code == 200
    assert client.get(f"/api/communication-rubric/versions/{versions[1]['id']}", headers=marker).status_code == 200
    restore = f"/api/admin/communication-rubric/versions/{versions[1]['id']}/restore"
    assert client.post(restore, headers=marker).status_code == 403
    assert client.post(restore).status_code == 401
    assert client.get("/api/communication-rubric/versions").status_code == 401
    after = client.get("/api/communication-rubric/versions", headers=admin).json()["versions"]
    assert [v["versionNumber"] for v in after] == [2, 1]
