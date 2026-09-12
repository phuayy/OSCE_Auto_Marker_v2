import json
import runpy
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from app.core.scorer_utils import (
    extract_primary_json_dict_from_model_output,
    normalize_timestamp,
    read_json_transcript_text,
    safe_extract_payload,
)
from app.core.utils import write_text_atomic
from app.llm.base import ChatResponse, LLMValidationError

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def test_model_json_chooses_full_criteria_over_analysis_objects():
    raw = 'Analysis: {"plan": "score"}\n```json\n{"criteria": [{"id": 1}, {"id": 2}]}\n```\n{"criteria": []}'
    assert len(extract_primary_json_dict_from_model_output(raw)["criteria"]) == 2
    assert safe_extract_payload("no usable JSON")[1].startswith("Model JSON parse error:")
    assert safe_extract_payload('[{"criteria": []}]')[1] is not None


def test_transcript_timestamp_policies_and_legacy_text(tmp_path):
    path = tmp_path / "transcript.json"
    path.write_text(json.dumps({"segments": [{"speaker": "STUDENT", "start": 65.125, "end": 70.5, "text": "Hello"}]}))
    assert "00:01:05.125 - 00:01:10.500" in read_json_transcript_text(path)
    assert "00:01:05 - 00:01:10" in read_json_transcript_text(path, include_ms=False)
    path.write_text("Legacy plain transcript")
    assert read_json_transcript_text(path) == "Legacy plain transcript"
    assert normalize_timestamp(float("inf")) is None
    assert normalize_timestamp("at 01:05,125") == "00:01:05"


def test_atomic_writer_handles_threads_without_temporary_file_collisions(tmp_path):
    path = tmp_path / "scores.json"
    payloads = [json.dumps({"index": index, "text": "x" * 100_000}) for index in range(20)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda text: write_text_atomic(path, text), payloads))
    assert path.read_text() in payloads
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("script", ["nvidia_osce_assessor.py", "nvidia_osce_communication.py"])
def test_scorers_share_completion_and_preserve_validation_and_provenance(script, monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    module = runpy.run_path(str(SCRIPTS / script))

    class Router:
        def complete(self, request, *, validate):
            assert request.total_timeout_seconds > 0
            with pytest.raises(LLMValidationError):
                validate('{"criteria": []}')
            validate('{"criteria": [{"id": 1}]}')
            if script == "nvidia_osce_communication.py":
                assert request.reasoning.enabled == module["COMMUNICATION_ENABLE_THINKING"]
                assert request.modes() == module["communication_mode_ladder"]()
            return ChatResponse(content='{"criteria": [{"id": 1}]}', provider_id="fallback", model="model")

    response = module["create_completion"](Router(), [{"role": "user", "content": "score"}], expected_items=1)
    assert response.provider_id == "fallback"
    assert response.model == "model"


@pytest.mark.parametrize("script", [
    "nvidia_osce_assessor.py", "nvidia_osce_communication.py", "audio_professionalism_extractor.py",
])
def test_standalone_scorers_keep_help_and_invalid_input_exit_codes(script):
    help_result = subprocess.run([sys.executable, str(SCRIPTS / script), "--help"], capture_output=True, text=True, timeout=20)
    assert help_result.returncode == 0, help_result.stderr
    invalid = subprocess.run([sys.executable, str(SCRIPTS / script)], capture_output=True, text=True, timeout=20)
    assert invalid.returncode == 2
    assert "required" in invalid.stderr
