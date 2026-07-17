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


def test_clip_child_session_inherits_parent_corpus(tmp_path: Path) -> None:
    import copy
    from types import SimpleNamespace

    from app.core.utils import utc_now_iso
    from app.services.clip_service import ClipService

    class FakeSessions:
        def __init__(self, initial: dict) -> None:
            self.sessions = {str(initial["id"]): copy.deepcopy(initial)}

        async def read(self, session_id: str) -> dict:
            return copy.deepcopy(self.sessions[str(session_id)])

        async def write(self, session: dict) -> None:
            self.sessions[str(session["id"])] = copy.deepcopy(session)

        async def read_all_entries(self) -> list:
            return []

        async def ensure_names_for_index(self, entries: list) -> tuple[list, set]:
            return [], set()

        def reserve_unique_session_name(self, used_keys: set, preferred: str = "") -> str:
            return preferred or "Child"

        def public_session(self, session: dict) -> dict:
            return copy.deepcopy(session)

    class FakeJobs:
        async def enqueue(self, *_args, **_kwargs) -> dict:
            return {"id": "job-1", "status": "queued"}

        def public_job(self, job: dict) -> dict:
            return dict(job)

    clip_path = tmp_path / "clip-1.mp4"
    clip_path.write_bytes(b"clip")
    case_study_path = tmp_path / "case.pdf"
    case_study_path.write_bytes(b"%PDF-1.4")
    corpus_snapshot = {"id": "c1", "name": "Common Cold (URTI)", "terms": ["nasal block"]}
    parent = {
        "id": "parent-1",
        "name": "Parent",
        "status": "cropped",
        "workflow": "long",
        "corpus": corpus_snapshot,
        "files": {"caseStudy": {"originalName": "case.pdf", "absolutePath": str(case_study_path)}},
        "outputs": {
            "videoClips": [
                {"id": "clip-1", "label": "Student 1", "kind": "session", "absolutePath": str(clip_path)}
            ]
        },
    }
    sessions = FakeSessions(parent)
    settings = Settings(root_dir=tmp_path, backend_root=tmp_path, ffmpeg_bin="f", ffprobe_bin="f", scorer_python_bin="p")
    service = ClipService(
        sessions=sessions,
        events=SimpleNamespace(),
        media=SimpleNamespace(settings=settings),
        pipeline=SimpleNamespace(now_iso=utc_now_iso),
        jobs=FakeJobs(),
    )

    result = asyncio.run(service.assess_clip("parent-1", "clip-1", defer=True))

    child = sessions.sessions[str(result["session"]["id"])]
    assert child["corpus"] == corpus_snapshot
    assert child["parentSessionId"] == "parent-1"
