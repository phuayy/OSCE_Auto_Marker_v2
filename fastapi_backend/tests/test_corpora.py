from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from app.api.routes import corpora
from app.core.config import Settings
from app.core.exceptions import AppError
from app.database.orm import OrmDatabase
from app.repositories.corpus_repository import SEED_CORPORA, CorpusRepository
from app.schemas.corpora import normalize_corpus_terms
from app.services.container import create_container


def make_repository(tmp_path: Path) -> CorpusRepository:
    return CorpusRepository(OrmDatabase(tmp_path / "corpora.sqlite3"))


def build_client(tmp_path: Path) -> TestClient:
    settings = Settings(
        root_dir=tmp_path,
        backend_root=tmp_path,
        ffmpeg_bin="ffmpeg",
        ffprobe_bin="ffprobe",
        scorer_python_bin="python",
        app_database_url="",
        database_url="",
    )
    container = create_container(settings)
    asyncio.run(container.artifacts.ensure_storage_layout())
    asyncio.run(container.corpora.seed_defaults())

    app = FastAPI()
    app.state.container = container

    @app.exception_handler(AppError)
    async def _app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"error": exc.message})

    app.include_router(corpora.router, prefix="/api")
    return TestClient(app)


def test_seed_defaults_only_when_empty(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)

    assert asyncio.run(repository.seed_defaults()) == len(SEED_CORPORA)
    rows = asyncio.run(repository.list_rows())
    assert {row["name"] for row in rows} == {name for name, _ in SEED_CORPORA}
    assert any("nasal block" in row["terms"] for row in rows)

    # Deleting a seed must not resurrect it on re-seed (table is non-empty).
    deleted_id = rows[0]["id"]
    assert asyncio.run(repository.delete(deleted_id))
    assert asyncio.run(repository.seed_defaults()) == 0
    assert len(asyncio.run(repository.list_rows())) == len(SEED_CORPORA) - 1


def test_repository_crud_roundtrip(tmp_path: Path) -> None:
    repository = make_repository(tmp_path)

    created = asyncio.run(repository.create("Asthma", ["salbutamol", "wheezing"]))
    assert asyncio.run(repository.get(created["id"]))["terms"] == ["salbutamol", "wheezing"]

    updated = asyncio.run(repository.update(created["id"], "Asthma (Adult)", ["salbutamol"]))
    assert updated["name"] == "Asthma (Adult)"
    assert updated["terms"] == ["salbutamol"]

    assert asyncio.run(repository.delete(created["id"]))
    assert asyncio.run(repository.get(created["id"])) is None
    assert asyncio.run(repository.update("missing", "X", [])) is None


def test_corpora_routes_crud_and_conflicts(tmp_path: Path) -> None:
    client = build_client(tmp_path)

    listed = client.get("/api/corpora")
    assert listed.status_code == 200
    seed_names = {row["name"] for row in listed.json()["corpora"]}
    assert "Common Cold (URTI)" in seed_names

    created = client.post(
        "/api/corpora",
        json={"name": "  Diabetes  ", "terms": ["metformin", " metformin ", "", "insulin", "Metformin"]},
    )
    assert created.status_code == 200
    corpus = created.json()["corpus"]
    assert corpus["name"] == "Diabetes"
    # Trimmed, empties dropped, deduped case-insensitively, order preserved.
    assert corpus["terms"] == ["metformin", "insulin"]

    duplicate = client.post("/api/corpora", json={"name": "Diabetes", "terms": []})
    assert duplicate.status_code == 409

    updated = client.put(f"/api/corpora/{corpus['id']}", json={"name": "Diabetes", "terms": ["insulin"]})
    assert updated.status_code == 200
    assert updated.json()["corpus"]["terms"] == ["insulin"]

    assert client.put("/api/corpora/missing", json={"name": "X", "terms": []}).status_code == 404
    assert client.delete(f"/api/corpora/{corpus['id']}").status_code == 200
    assert client.delete(f"/api/corpora/{corpus['id']}").status_code == 404


def test_normalize_corpus_terms_handles_non_lists() -> None:
    assert normalize_corpus_terms(None) == []
    assert normalize_corpus_terms("nasal block") == []
    assert normalize_corpus_terms([" nasal block ", 42]) == ["nasal block", "42"]
