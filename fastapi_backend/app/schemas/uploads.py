from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from app.domain.enums import SegmentationMethod, UploadFileKind, Workflow
from app.pipeline import person_presets
from app.pipeline import region_focus

UploadWorkflow = Workflow


class PersonSegmentationOptions(BaseModel):
    """Occupancy rule for the person detector, chosen per upload.

    The camera angle decides how many people are on screen during a station and
    how much of the frame a real person fills, and no single rule covers every
    rig — hence a preset per scenario rather than a server-wide constant. The
    numbers are only accepted for ``preset="custom"``: a named preset that
    silently carried different numbers than the catalogue advertises would make
    the label meaningless.
    """

    preset: str = person_presets.DEFAULT_PRESET
    minPeople: int | None = Field(
        default=None,
        ge=person_presets.MIN_PEOPLE_RANGE[0],
        le=person_presets.MIN_PEOPLE_RANGE[1],
    )
    minBoxHeightRatio: float | None = Field(
        default=None,
        ge=person_presets.MIN_BOX_HEIGHT_RATIO_RANGE[0],
        le=person_presets.MIN_BOX_HEIGHT_RATIO_RANGE[1],
    )
    minSessionSeconds: float | None = Field(
        default=None,
        ge=person_presets.MIN_SESSION_SECONDS_RANGE[0],
        le=person_presets.MIN_SESSION_SECONDS_RANGE[1],
    )

    @field_validator("preset", mode="before")
    @classmethod
    def normalize_preset(cls, value: Any) -> str:
        cleaned = str(value or "").strip().lower().replace("-", "_")
        if not cleaned:
            return person_presets.DEFAULT_PRESET
        if cleaned not in person_presets.PRESET_IDS:
            raise ValueError(
                f"Unknown segmentation preset '{cleaned}'. "
                f"Expected one of: {', '.join(person_presets.PRESET_IDS)}."
            )
        return cleaned

    @model_validator(mode="after")
    def numbers_belong_to_custom(self) -> "PersonSegmentationOptions":
        if self.preset != person_presets.CUSTOM_PRESET:
            self.minPeople = None
            self.minBoxHeightRatio = None
            self.minSessionSeconds = None
        return self

    def resolved(self) -> dict[str, object]:
        """Concrete numbers to persist on the session and hand to the detector."""
        return person_presets.resolve_options(self.model_dump(), strict=True)


class RegionFocusOptions(BaseModel):
    """Horizontal region-of-interest for the person detector, chosen per upload.

    Independent of the occupancy preset above: a camera rig can frame a third
    party (another examiner, a doorway) at one edge of the shot close enough
    to pass the occupancy rule's own height gate. Restricting detection to the
    side of the frame the intended subjects occupy is the fix for that, and it
    applies underneath whichever preset was chosen.
    """

    leftEnabled: bool = region_focus.DEFAULT_LEFT_ENABLED
    rightEnabled: bool = region_focus.DEFAULT_RIGHT_ENABLED
    leftRatio: float = Field(
        default=region_focus.DEFAULT_LEFT_RATIO,
        ge=region_focus.RATIO_RANGE[0],
        le=region_focus.RATIO_RANGE[1],
    )
    rightRatio: float = Field(
        default=region_focus.DEFAULT_RIGHT_RATIO,
        ge=region_focus.RATIO_RANGE[0],
        le=region_focus.RATIO_RANGE[1],
    )

    @model_validator(mode="after")
    def at_least_one_zone_enabled(self) -> "RegionFocusOptions":
        if not self.leftEnabled and not self.rightEnabled:
            raise ValueError("At least one of leftEnabled/rightEnabled must be true.")
        return self

    def resolved(self) -> dict[str, object]:
        """Concrete numbers to persist on the session and hand to the detector."""
        return region_focus.resolve_options(self.model_dump(), strict=True)


def coerce_segmentation_options(value: Any) -> Any:
    """Accept the options as an object or as a JSON string.

    The async initiate path sends JSON; the legacy multipart form can only send
    strings, and both must land on the same validated model rather than on two
    divergent parsers.
    """
    if value is None or value == "":
        return None
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("segmentationOptions must be a JSON object.") from error
        if not isinstance(decoded, dict):
            raise ValueError("segmentationOptions must be a JSON object.")
        return decoded
    return value


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
    """Validated upload metadata for the chunked upload's ``initiate`` call.

    A mixin rather than plain fields on the request because these invariants —
    a trimmed, bounded name; a known workflow; a segmentation method that only
    means anything to a long upload — belong to the *session being created*,
    not to one transport. It stays a mixin so a second ingest path cannot
    reintroduce its own copy of them.
    """

    workflow: UploadWorkflow = Workflow.STANDARD
    # Optional user-chosen session name; blank/None falls back to an
    # auto-generated name. The session service de-duplicates it on save.
    # Over-long names are silently truncated by the validator, not rejected.
    sessionName: str | None = None
    # Long-workflow only: which auto-crop segmentation method to use.
    segmentation: SegmentationMethod | None = None
    # Person-segmentation only: the occupancy rule for this camera angle.
    # None = the detector's own default preset.
    segmentationOptions: PersonSegmentationOptions | None = None
    # Person-segmentation only: which horizontal zone(s) of the frame to
    # detect in. None = the detector's own default (the whole frame).
    regionFocusOptions: RegionFocusOptions | None = None
    # Optional transcription-corpus id whose terms bias WhisperX for the whole
    # session (and every clip child). None/""/"none" = plain transcription.
    corpusId: str | None = None

    @field_validator("workflow", mode="before")
    @classmethod
    def normalize_workflow(cls, value: Any) -> str:
        cleaned = str(value or "").strip().lower()
        return cleaned or Workflow.STANDARD

    @field_validator("sessionName", mode="before")
    @classmethod
    def trim_session_name(cls, value: Any) -> str | None:
        if value in {None, ""}:
            return None
        cleaned = str(value).strip()[:SESSION_NAME_MAX_LENGTH]
        return cleaned or None

    @field_validator("corpusId", mode="before")
    @classmethod
    def trim_corpus_id(cls, value: Any) -> str | None:
        if value in {None, ""}:
            return None
        cleaned = str(value).strip()
        if not cleaned or cleaned.lower() == "none":
            return None
        return cleaned[:64]

    @field_validator("segmentation", mode="before")
    @classmethod
    def normalize_segmentation(cls, value: Any) -> str | None:
        if value in {None, ""}:
            return None
        cleaned = str(value).strip().lower()
        # Accept the natural synonym from older clients/scripts.
        if cleaned == "human":
            return SegmentationMethod.PERSON
        return cleaned or None

    @field_validator("segmentationOptions", mode="before")
    @classmethod
    def parse_segmentation_options(cls, value: Any) -> Any:
        return coerce_segmentation_options(value)

    @field_validator("regionFocusOptions", mode="before")
    @classmethod
    def parse_region_focus_options(cls, value: Any) -> Any:
        return coerce_segmentation_options(value)

    @model_validator(mode="after")
    def segmentation_only_for_long_workflow(self) -> "UploadMetadataMixin":
        if self.workflow != Workflow.LONG:
            self.segmentation = None
        # The occupancy rule and the region focus only mean anything to the
        # person detector. Kept when no method was chosen: the server default
        # may itself be "person".
        if self.segmentation == SegmentationMethod.BELLS or self.workflow != Workflow.LONG:
            self.segmentationOptions = None
            self.regionFocusOptions = None
        return self

    def resolved_segmentation_options(self) -> dict[str, object] | None:
        """Numbers to persist on the session, or None when they do not apply.

        Resolved at upload time rather than at job time so a run is reproducible
        and auditable: the session records what the preset meant on the day it
        was chosen, not what the table says whenever the job happens to run.
        """
        if self.segmentationOptions is None:
            return None
        return self.segmentationOptions.resolved()

    def resolved_region_focus_options(self) -> dict[str, object] | None:
        """Numbers to persist on the session, or None when they do not apply.

        Same rationale as ``resolved_segmentation_options``: resolved once at
        upload time so the session records what was chosen, not whatever the
        default happens to be whenever the job eventually runs.
        """
        if self.regionFocusOptions is None:
            return None
        return self.regionFocusOptions.resolved()


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
