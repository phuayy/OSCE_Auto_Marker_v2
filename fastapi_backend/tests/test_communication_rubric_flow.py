"""The whole communication rubric flow, with the real parser subprocess and the
real scorer validation: a PDF becomes a draft, an administrator edits it
(criteria and the points per label), the save activates it, and a scoring run
marks against exactly what was saved.

Only the asset registry is faked; the parser runs as the API would run it.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from fastapi import UploadFile

from app.core.config import Settings
from app.core.process import CommandRunner
from app.domain.communication_rubric import rubric_fingerprint
from app.services.artifact_service import ArtifactService
from app.services.rubric_service import RubricService

REPO = Path(__file__).resolve().parents[2]
SHIPPED_PDF = REPO / "rubrics" / "PHR1012 OSCE Rubric.pdf"
ADMIN = SimpleNamespace(to_provenance=lambda: {"userId": "u1", "username": "admin", "displayName": "Dr A"})


class _Assets:
    async def register_communication_rubric(self, *, path: Path, original_name: str, mime_type: str | None):
        return SimpleNamespace(
            asset={"id": "asset-1", "absolutePath": str(path), "contentSha256": "sha-1"}, is_duplicate=False
        )


def _service(tmp_path: Path) -> RubricService:
    root = tmp_path / "root"
    root.mkdir()
    (root / "scripts").symlink_to(REPO / "scripts")
    settings = Settings(root_dir=root, backend_root=root, scorer_python_bin=sys.executable)
    settings.paths.ensure_layout()
    return RubricService(settings, CommandRunner(root), ArtifactService(settings), _Assets())  # type: ignore[arg-type]


def _scorer():
    scripts = str(REPO / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module("nvidia_osce_communication")


def test_draft_edit_save_then_score_against_the_saved_rubric(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("RUBRIC_LLM_FALLBACK", "false")  # the shipped PDF parses deterministically
    svc = _service(tmp_path)
    active_json = svc.settings.paths.communication_rubric_json_path

    # 1. Extract into a draft (auto: deterministic first). Nothing is active yet.
    with SHIPPED_PDF.open("rb") as handle:
        draft = asyncio.run(
            svc.extract_draft(UploadFile(file=handle, filename="rubric.pdf", headers={"content-type": "application/pdf"}),
                              mode="auto", actor=ADMIN)
        )
    assert not active_json.exists()
    extracted = draft["rubric"]
    assert extracted["criteria_count"] == 7 and extracted["max_score"] == 21 and extracted["pass_threshold"] == 11

    # 2. The administrator fixes a label, drops the last criterion and changes the points.
    criteria = [
        {"section": c["section"], "label": c["label"], "indicators": c["indicators"]}
        for c in extracted["criteria"][:-1]
    ]
    criteria[0]["label"] = "Opens the consultation"
    edited = {
        "title": extracted["title"],
        "notes": extracted["notes"],
        "scoring_scale": {"All": 4, "Most": 3, "Some": 1, "None": 0},
        "pass_threshold": 15,
        "criteria": criteria,
    }
    saved = asyncio.run(svc.save_edited(edited, draft_id=draft["draft"]["draftId"], actor=ADMIN))["rubric"]

    # 3. What is now active is exactly what was saved, with provenance.
    on_disk = json.loads(active_json.read_text(encoding="utf-8"))
    assert on_disk == saved
    assert on_disk["criteria_count"] == 6 and on_disk["max_score"] == 24 and on_disk["pass_threshold"] == 15
    assert on_disk["criteria"][0]["label"] == "Opens the consultation"
    assert [c["id"] for c in on_disk["criteria"]] == [1, 2, 3, 4, 5, 6]
    assert on_disk["extraction"]["method"] == "edited"
    assert on_disk["extraction"]["edited_by"]["username"] == "admin"
    assert on_disk["extraction"]["source"] == {"method": "deterministic"}
    assert on_disk["rubric_revision"] == rubric_fingerprint(on_disk)
    assert svc.settings.paths.communication_rubric_pdf_path.read_bytes() == SHIPPED_PDF.read_bytes()
    assert list(svc.drafts_dir.iterdir()) == []

    # 4. A scoring run reads that file and marks with its scale and pass mark.
    scorer = _scorer()
    rubric = scorer.read_parsed_rubric(active_json)
    labels = ["All", "All", "Most", "Some", "None", "All"]
    reply = {
        "session_id": "s1",
        "overall_summary": "ok",
        "criteria": [
            {"score_label": label, "evidence": "Said so.", "timestamp": "00:01:00"} for label in labels
        ],
    }
    sheet, issues = scorer.validate_output(reply, rubric)
    assert issues == []
    assert [c["points"] for c in sheet["criteria"]] == [4, 4, 3, 1, 0, 4]
    assert sheet["scoring_scale"] == {"All": 4, "Most": 3, "Some": 1, "None": 0}
    summary = sheet["scoring_summary"]
    assert summary["total_score"] == 16 and summary["max_score"] == 24 and summary["pass_threshold"] == 15
    assert summary["pass_fail"] == "Pass"
    assert sheet["rubric_extraction"]["method"] == "edited"

    # 5. Editing in place afterwards keeps the PDF and asset, and re-stamps the revision.
    edited["pass_threshold"] = 17
    again = asyncio.run(svc.save_edited(edited, actor=ADMIN))["rubric"]
    assert again["rubric_asset_id"] == on_disk["rubric_asset_id"]
    assert again["pass_threshold"] == 17 and again["rubric_revision"] != on_disk["rubric_revision"]
    assert again["extraction"]["source"] == {"method": "deterministic"}
    sheet, _ = scorer.validate_output(reply, scorer.read_parsed_rubric(active_json))
    assert sheet["scoring_summary"]["pass_fail"] == "Fail"
