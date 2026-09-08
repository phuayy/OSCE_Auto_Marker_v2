from __future__ import annotations


class AppError(Exception):
    """A failure the application recognises and describes for the caller.

    ``retryable`` is what the job queue consults. It defaults to the HTTP
    reading — a 5xx is a server-side problem worth attempting again, a 4xx is a
    rejection that will be rejected identically next time — but a subclass may
    override it: a host that ran out of memory reports a 5xx and still gains
    nothing from an immediate re-run on the same machine.
    """

    def __init__(self, message: str, status_code: int = 500, *, retryable: bool | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.retryable = (status_code >= 500) if retryable is None else retryable


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


class TranscriptionResourceError(AppError):
    """The host could not give the transcription engine the memory it needs.

    Raised when a model load dies of exhaustion rather than of anything about
    the recording: a Windows commit limit ("The paging file is too small for
    this operation to complete", OS error 1455), a CPU allocator refusal, or a
    CUDA out-of-memory the engine could not fall back from — or the same
    shortage arriving as a crash rather than an exception, when the allocator
    faults on memory the OS refused and the subprocess dies with an access
    violation before any handler runs.

    It is a 507 — the server, not the request, is short — but deliberately
    **not retryable**: RAM, VRAM and the pagefile are the same size on the next
    attempt, so a re-run costs another model load and fails identically. The
    message is written for the operator, because fixing it means changing the
    machine or the engine selection.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=507, retryable=False)
