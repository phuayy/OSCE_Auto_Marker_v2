"""Persisted and API vocabulary. Values remain compatible with existing records."""

from enum import StrEnum


class PipelineStep(StrEnum):
    WHISPERX = "whisperx"
    AUDIO_EXTRACTION = "audio_extraction"
    TRANSCRIPTION = "transcription"
    TRANSCRIPT_NORMALIZATION = "transcript_normalization"
    LLM_PREPROCESS = "llm_preprocess"
    AUDIO_PROFESSIONALISM = "audio_professionalism"
    COMMUNICATION_SCORING = "communication_scoring"
    CONTENT_SCORING = "content_scoring"
    ASSESSMENT_PERSISTENCE = "assessment_persistence"
    PERSON_DETECTION = "person_detection"
    BELL_DETECTION = "bell_detection"


class StepStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class UploadStatus(StrEnum):
    INITIATED = "initiated"
    UPLOADING = "uploading"
    ASSEMBLING = "assembling"
    COMMITTED = "committed"
    FAILED = "failed"
    ABORTED = "aborted"
    EXPIRED = "expired"


class ClipExportStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ClipExportScope(StrEnum):
    """How much of a session's clip list one export job cuts.

    ``plan`` is a whole split — every session clip in the current plan, which
    is what "Export clips" queues. ``clip`` is a re-cut of individual clips
    whose boundaries changed after the split; it runs through the same job so
    a recrop is durable, resumable and gauged exactly like an export, but the
    editor keeps its selection instead of being handed a fresh clip list.
    """

    PLAN = "plan"
    CLIP = "clip"


class AssessmentResultStatus(StrEnum):
    """Status of one scorer's row in ``assessment_results`` — a vocabulary of
    its own, not ``SessionStatus``/``StepStatus``: by the time a scorer's
    output reaches ``AssessmentService``, the step that produced it has
    already succeeded, so today this only ever takes one value."""

    COMPLETED = "completed"


class TaskType(StrEnum):
    PROCESS_SESSION = "process_session"
    AUTO_CROP = "auto_crop"
    EXPORT_CLIPS = "export_clips"


class Workflow(StrEnum):
    STANDARD = "standard"
    LONG = "long"


class SegmentationMethod(StrEnum):
    BELLS = "bells"
    PERSON = "person"


class ClipKind(StrEnum):
    SESSION = "session"
    INTERMISSION = "intermission"


class UploadFileKind(StrEnum):
    VIDEO = "video"
    CASE_STUDY = "caseStudy"


class ScoreDisplay(StrEnum):
    """How the screens present a score: a share of the maximum, or the points
    themselves. A per-account display preference (see CLAUDE.md "Two-tier
    settings"); nothing stored about a session depends on it."""

    PERCENT = "percent"
    RAW = "raw"


class OutputKey(StrEnum):
    AUDIO = "audio"
    WHISPERX_JSON = "whisperxJson"
    TRANSCRIPT = "transcript"
    SUBTITLE = "subtitle"
    SUBTITLE_TRACK = "subtitleTrack"
    AUDIO_PROFESSIONALISM = "audioProfessionalism"
    COMMUNICATION_SCORES = "communicationScores"
    VIDEO_CLIPS = "videoClips"
    SCORES = "scores"
