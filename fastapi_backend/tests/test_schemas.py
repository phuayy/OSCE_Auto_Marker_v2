from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.uploads import InitiateUploadRequest


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


def test_initiate_rejects_unknown_segmentation_rather_than_degrading() -> None:
    """The removed legacy route degraded an unknown segmentation method to the
    server default, because failing there cost the client a multi-gigabyte
    body it had already sent. ``initiate`` carries no bytes, so it refuses:
    a typo must not silently become a different segmentation run."""
    with pytest.raises(ValidationError):
        InitiateUploadRequest(**_initiate_payload(workflow="long", segmentation="lasers"))


def test_initiate_defaults_to_the_standard_workflow() -> None:
    request = InitiateUploadRequest(**_initiate_payload(workflow=None, sessionName=None, segmentation=None))
    assert request.workflow == "standard"
    assert request.sessionName is None
    assert request.segmentation is None
