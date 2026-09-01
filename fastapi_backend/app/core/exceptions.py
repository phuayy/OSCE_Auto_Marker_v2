from __future__ import annotations


class AppError(Exception):
    def __init__(self, message: str, status_code: int = 500) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class EmptyTranscriptError(AppError):
    """Transcription produced no usable speech segments.

    The transcript is the only input the three scorers read, so an empty one
    lets the pipeline emit a confident-looking assessment of nothing. This is a
    permanent condition for a given recording — re-running the same audio
    produces the same empty result — so it is surfaced as a 4xx and is never
    re-queued by the local retry policy.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=422)
