from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


UploadWorkflow = Literal["standard", "long"]
UploadFileKind = Literal["video", "caseStudy"]
# How a long upload is auto-split into student clips: bell sounds (audio) or
# person presence (RT-DETR vision). None lets the server default decide.
SegmentationMethod = Literal["bells", "person"]


class InitiateUploadFile(BaseModel):
    kind: UploadFileKind
    originalName: str = Field(min_length=1, max_length=255)
    mimeType: str = Field(default="", max_length=160)
    sizeBytes: int = Field(gt=0)
    sha256: str | None = Field(default=None, max_length=64)

    @field_validator("originalName", mode="before")
    @classmethod
    def trim_original_name(cls, value: Any) -> str:
        cleaned = str(value or "").strip()
        if not cleaned:
            raise ValueError("File name is required.")
        return cleaned[:255]

    @field_validator("sha256", mode="before")
    @classmethod
    def validate_sha256(cls, value: Any) -> str | None:
        if value in {None, ""}:
            return None
        cleaned = str(value).strip().lower()
        if not re.fullmatch(r"[a-f0-9]{64}", cleaned):
            raise ValueError("sha256 must be a 64-character lowercase hex digest.")
        return cleaned


# Matches Settings.session_name_max_length — the service truncates to this, so
# accepting more here would just truncate silently at a different layer.
SESSION_NAME_MAX_LENGTH = 80


class UploadMetadataMixin(BaseModel):
    """Shared, validated upload metadata (async initiate + legacy multipart)."""

    workflow: UploadWorkflow = "standard"
    # Optional user-chosen session name; blank/None falls back to an
    # auto-generated name. The session service de-duplicates it on save.
    sessionName: str | None = Field(default=None, max_length=SESSION_NAME_MAX_LENGTH)
    # Long-workflow only: which auto-crop segmentation method to use.
    segmentation: SegmentationMethod | None = None

    @field_validator("workflow", mode="before")
    @classmethod
    def normalize_workflow(cls, value: Any) -> str:
        cleaned = str(value or "").strip().lower()
        return cleaned or "standard"

    @field_validator("sessionName", mode="before")
    @classmethod
    def trim_session_name(cls, value: Any) -> str | None:
        if value in {None, ""}:
            return None
        cleaned = str(value).strip()[:SESSION_NAME_MAX_LENGTH]
        return cleaned or None

    @field_validator("segmentation", mode="before")
    @classmethod
    def normalize_segmentation(cls, value: Any) -> str | None:
        if value in {None, ""}:
            return None
        cleaned = str(value).strip().lower()
        # Accept the natural synonym from older clients/scripts.
        if cleaned == "human":
            return "person"
        return cleaned or None

    @model_validator(mode="after")
    def segmentation_only_for_long_workflow(self) -> "UploadMetadataMixin":
        if self.workflow != "long":
            self.segmentation = None
        return self


class LegacyUploadForm(UploadMetadataMixin):
    """Validated form fields of the legacy single-shot POST /upload.

    The legacy route is a compatibility fallback for older clients, so an
    unknown segmentation value degrades to None (server default) instead of
    rejecting the whole multi-gigabyte upload.
    """

    @field_validator("segmentation", mode="before")
    @classmethod
    def drop_unknown_segmentation(cls, value: Any) -> str | None:
        if value in {None, ""}:
            return None
        cleaned = str(value).strip().lower()
        if cleaned == "human":
            cleaned = "person"
        return cleaned if cleaned in {"bells", "person"} else None


class InitiateUploadRequest(UploadMetadataMixin):
    files: list[InitiateUploadFile] = Field(min_length=2, max_length=2)
    autoProcess: bool = True

    @model_validator(mode="after")
    def require_video_and_case_study(self) -> "InitiateUploadRequest":
        files = self.files
        kinds = [getattr(item, "kind", "") for item in files]
        if kinds.count("video") != 1:
            raise ValueError("Exactly one video file is required.")
        if kinds.count("caseStudy") != 1:
            raise ValueError("Exactly one caseStudy PDF file is required.")
        return self


class CompleteUploadRequest(BaseModel):
    autoProcess: bool | None = None


class UploadInitiateResponse(BaseModel):
    session: dict[str, object]
    uploadId: str
    strategy: str
    partSizeBytes: int
    expiresAt: str
    fileUploads: list[dict[str, object]]
    job: dict[str, object] | None = None


class UploadStatusResponse(BaseModel):
    upload: dict[str, object]


class UploadCompleteResponse(BaseModel):
    session: dict[str, object]
    upload: dict[str, object]
    job: dict[str, object] | None = None
