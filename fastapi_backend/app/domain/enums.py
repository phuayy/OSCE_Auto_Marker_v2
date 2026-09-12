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
