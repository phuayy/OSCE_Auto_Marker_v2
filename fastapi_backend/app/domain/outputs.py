from dataclasses import dataclass

from app.domain.enums import OutputKey, PipelineStep


@dataclass(frozen=True)
class OutputSpec:
    key: OutputKey
    step: PipelineStep
    media_directory: str
    started_event: str
    completed_event: str


OUTPUT_SPECS = {
    OutputKey.AUDIO_PROFESSIONALISM: OutputSpec(
        OutputKey.AUDIO_PROFESSIONALISM, PipelineStep.AUDIO_PROFESSIONALISM,
        "/media/audio-professionalism", "audio_professionalism_started", "audio_professionalism_complete",
    ),
    OutputKey.COMMUNICATION_SCORES: OutputSpec(
        OutputKey.COMMUNICATION_SCORES, PipelineStep.COMMUNICATION_SCORING,
        "/media/communication-scores", "communication_scoring_started", "communication_scoring_complete",
    ),
    OutputKey.SCORES: OutputSpec(
        OutputKey.SCORES, PipelineStep.CONTENT_SCORING,
        "/media/scores", "scoring_started", "scored",
    ),
}
