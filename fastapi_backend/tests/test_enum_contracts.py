import subprocess
import sys
from pathlib import Path

import pytest
from app.domain.enums import ClipKind, PipelineStep, StepStatus, Workflow
from app.schemas.uploads import InitiateUploadRequest


def test_browser_enums_match_backend() -> None:
    root = Path(__file__).resolve().parents[2]
    subprocess.run([sys.executable, str(root / "scripts/generate_enums.py"), "--check"], check=True)


def test_wire_values_remain_strings() -> None:
    assert Workflow.LONG == "long"
    assert StepStatus.FAILED == "failed"
    assert PipelineStep.WHISPERX == "whisperx"
    assert ClipKind.SESSION == "session"


@pytest.mark.parametrize("value", ["unknown", "", "longer"])
def test_invalid_workflow_is_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        InitiateUploadRequest.model_validate({"workflow": value, "files": []})
