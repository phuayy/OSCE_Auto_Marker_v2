from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.sessions import ManualClipsRequest, RecropClipRequest
from app.schemas.uploads import InitiateUploadRequest, LegacyUploadForm


def _initiate_payload(**overrides):
    payload = {
        "workflow": "standard",
        "autoProcess": True,
        "files": [
            {"kind": "video", "originalName": "station.mp4", "mimeType": "video/mp4", "sizeBytes": 5},
            {"kind": "caseStudy", "originalName": "case.pdf", "mimeType": "application/pdf", "sizeBytes": 4},
        ],
    }
    payload.update(overrides)
    return payload


def test_initiate_session_name_is_trimmed_and_capped_to_service_limit() -> None:
    request = InitiateUploadRequest(**_initiate_payload(sessionName="  " + "x" * 200 + "  "))
    assert request.sessionName == "x" * 80


def test_initiate_rejects_unknown_workflow() -> None:
    with pytest.raises(ValidationError):
        InitiateUploadRequest(**_initiate_payload(workflow="turbo"))


def test_initiate_drops_segmentation_for_standard_workflow() -> None:
    request = InitiateUploadRequest(**_initiate_payload(segmentation="bells"))
    assert request.segmentation is None


def test_initiate_keeps_segmentation_for_long_workflow_and_maps_synonym() -> None:
    request = InitiateUploadRequest(**_initiate_payload(workflow="long", segmentation="human"))
    assert request.segmentation == "person"


def test_legacy_form_defaults_and_degradation() -> None:
    form = LegacyUploadForm(sessionName=None, workflow=None, segmentation=None)
    assert form.workflow == "standard"
    assert form.sessionName is None
    assert form.segmentation is None
    # Unknown segmentation degrades to None on the compat route instead of
    # failing a multi-gigabyte upload.
    degraded = LegacyUploadForm(sessionName=" S1 ", workflow="long", segmentation="lasers")
    assert degraded.segmentation is None
    assert degraded.sessionName == "S1"
    assert degraded.workflow == "long"


def test_recrop_rejects_non_finite_and_inverted_bounds() -> None:
    with pytest.raises(ValidationError):
        RecropClipRequest(start=float("nan"), end=1.0)
    with pytest.raises(ValidationError):
        RecropClipRequest(start=float("inf"), end=1.0)
    with pytest.raises(ValidationError):
        RecropClipRequest(start=5.0, end=5.0)
    with pytest.raises(ValidationError):
        RecropClipRequest(start=-1.0, end=5.0)
    ok = RecropClipRequest(start=0.0, end=12.5)
    assert ok.end == 12.5


def test_manual_clips_rejects_bad_boundaries() -> None:
    with pytest.raises(ValidationError):
        ManualClipsRequest(boundaries=[10.0, float("nan")])
    with pytest.raises(ValidationError):
        ManualClipsRequest(boundaries=[-3.0])
    with pytest.raises(ValidationError):
        ManualClipsRequest(boundaries=list(float(i) for i in range(201)))
    ok = ManualClipsRequest(boundaries=[30.0, 60.0], labels=["  Student 1  "])
    assert ok.labels == ["Student 1"]


def test_manual_clips_kinds_normalized_and_validated() -> None:
    ok = ManualClipsRequest(boundaries=[30.0], kinds=[" Session ", "INTERMISSION"])
    assert ok.kinds == ["session", "intermission"]
    assert ManualClipsRequest(boundaries=[30.0]).kinds == []  # optional — old clients
    with pytest.raises(ValidationError):
        ManualClipsRequest(boundaries=[30.0], kinds=["break"])
