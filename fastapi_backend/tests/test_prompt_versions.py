from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from app.api.dependencies import require_admin
from app.api.routes import prompt_versions
from app.core.config import Settings
from app.core.exceptions import AppError
from app.core.process import CommandResult
from app.database.orm import OrmDatabase
from app.repositories.prompt_version_repository import PromptCatalogEntry, PromptVersionRepository
from app.services.prompt_registry_service import PromptRegistryService
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

ROOT_DIR = Path(__file__).resolve().parents[2]


def make_repository(tmp_path: Path) -> PromptVersionRepository:
    return PromptVersionRepository(OrmDatabase(tmp_path / "prompts.sqlite3"))


class FakeRunner:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.calls: list[dict] = []

    async def run(self, command, args, label, **kwargs) -> CommandResult:
        self.calls.append({"command": command, "args": list(args), "label": label, **kwargs})
        return CommandResult(stdout=self.stdout, stderr="")


def build_client(repository: PromptVersionRepository) -> TestClient:
    settings = Settings(root_dir=ROOT_DIR, backend_root=ROOT_DIR / "fastapi_backend", scorer_python_bin="python")
    service = PromptRegistryService(repository, FakeRunner(json.dumps({"entries": []})), settings)

    app = FastAPI()

    class _Container:
        pass

    container = _Container()
    container.prompt_registry = service
    app.state.container = container

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.message})

    app.include_router(prompt_versions.router, prefix="/api")
    app.dependency_overrides[require_admin] = lambda: {"sub": "test-admin", "username": "admin", "role": "admin"}
    return TestClient(app)


def test_record_if_new_inserts_then_is_idempotent(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    entries = [PromptCatalogEntry(key="k1", version="v1", text="hello", source_script="s.py")]

    first = asyncio.run(repository.record_if_new(entries))
    assert first.inserted == ["k1@v1"]
    assert first.unchanged == []
    assert first.drifted == []

    second = asyncio.run(repository.record_if_new(entries))
    assert second.inserted == []
    assert second.unchanged == ["k1@v1"]


def test_record_if_new_detects_drift_without_overwriting(tmp_path: Path, caplog) -> None:
    repository = make_repository(tmp_path)
    asyncio.run(
        repository.record_if_new([PromptCatalogEntry(key="k1", version="v1", text="hello", source_script="s.py")])
    )

    with caplog.at_level(logging.WARNING):
        drifted = asyncio.run(
            repository.record_if_new(
                [PromptCatalogEntry(key="k1", version="v1", text="a different wording", source_script="s.py")]
            )
        )
    assert drifted.inserted == []
    assert drifted.unchanged == []
    assert drifted.drifted == ["k1@v1"]
    assert "was not bumped" in caplog.text

    # The originally stored snapshot must survive untouched.
    rows = asyncio.run(repository.list_versions())
    assert len(rows) == 1
    stored = asyncio.run(repository.get(rows[0]["id"]))
    assert stored["templateText"] == "hello"


def test_list_versions_filters_and_omits_text(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    asyncio.run(
        repository.record_if_new(
            [
                PromptCatalogEntry(key="a", version="v1", text="text-a", source_script="s.py"),
                PromptCatalogEntry(key="b", version="v1", text="text-b", source_script="s.py"),
            ]
        )
    )

    all_rows = asyncio.run(repository.list_versions())
    assert {row["promptKey"] for row in all_rows} == {"a", "b"}
    assert all("templateText" not in row for row in all_rows)

    filtered = asyncio.run(repository.list_versions(prompt_key="a"))
    assert [row["promptKey"] for row in filtered] == ["a"]

    assert asyncio.run(repository.get("missing-id")) is None


def test_service_sync_from_scripts_parses_runner_output_and_records(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    settings = Settings(root_dir=ROOT_DIR, backend_root=ROOT_DIR / "fastapi_backend", scorer_python_bin="python")
    runner = FakeRunner(
        json.dumps({"entries": [{"key": "x.system", "version": "v1", "text": "hi", "sourceScript": "x.py"}]})
    )
    service = PromptRegistryService(repository, runner, settings)

    result = asyncio.run(service.sync_from_scripts())

    assert result.inserted == ["x.system@v1"]
    assert len(runner.calls) == 1
    assert runner.calls[0]["args"] == [str(settings.prompt_catalog_script_path)]
    assert runner.calls[0]["command"] == settings.scorer_python_bin

    rows = asyncio.run(repository.list_versions(prompt_key="x.system"))
    assert rows[0]["version"] == "v1"


def test_routes_list_and_get_and_missing(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)
    asyncio.run(
        repository.record_if_new([PromptCatalogEntry(key="k1", version="v1", text="hello", source_script="s.py")])
    )
    client = build_client(repository)
    # Swap in the seeded repository (build_client's own service starts empty).
    client.app.state.container.prompt_registry.repository = repository

    listed = client.get("/api/admin/prompt-versions")
    assert listed.status_code == 200
    rows = listed.json()["promptVersions"]
    assert len(rows) == 1
    assert "templateText" not in rows[0]

    record_id = rows[0]["id"]
    fetched = client.get(f"/api/admin/prompt-versions/{record_id}")
    assert fetched.status_code == 200
    assert fetched.json()["promptVersion"]["templateText"] == "hello"

    assert client.get("/api/admin/prompt-versions/missing-id").status_code == 404

    filtered = client.get("/api/admin/prompt-versions", params={"prompt_key": "k1"})
    assert len(filtered.json()["promptVersions"]) == 1
    empty = client.get("/api/admin/prompt-versions", params={"prompt_key": "nope"})
    assert empty.json()["promptVersions"] == []
