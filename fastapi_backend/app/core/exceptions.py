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


class TranscriptionEngineUnavailableError(AppError):
    """The selected transcription engine cannot run in this deployment at all.

    Raised when the engine's own availability probe says no — its optional
    dependency group is not installed (``uv sync`` without ``--group canary``
    removes NeMo, because a uv sync is exact rather than additive), its
    subprocess script is missing, or its binary is not configured. Unlike
    :class:`TranscriptionResourceError` this has nothing to do with the host's
    memory: the engine is simply not here, and it will be just as absent on the
    next attempt, so it is a 503 that is **not retryable**. The message carries
    the engine's own install instructions, because fixing it means changing the
    environment or the engine selection, never the recording.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=503, retryable=False)


# The failures the transcription router hands to the default engine: the host
# cannot run the selected engine — out of memory, or not installed — and would
# refuse identically on every retry, so a second engine is the only thing that
# turns the session into a transcript.
HOST_CANNOT_RUN_ENGINE_ERRORS = (TranscriptionResourceError, TranscriptionEngineUnavailableError)


class StaleSessionError(AppError):
    """A session write was based on a version the database has since moved past.

    Raised by :meth:`SessionRepository.write` when the ``_loadedUpdatedAt`` a
    caller read does not match the row's current ``updated_at``. Another writer
    committed in between; overwriting would silently discard their change.
    Callers that can rebuild their change from a fresh read should go through
    :meth:`SessionService.update`, which retries the mutation for them.
    """

    def __init__(self, session_id: str, *, loaded_version: str | None, current_version: str | None) -> None:
        super().__init__(
            f"Session {session_id} changed while it was being edited; the edit was not applied.",
            status_code=409,
            retryable=True,
        )
        self.session_id = session_id
        self.loaded_version = loaded_version
        self.current_version = current_version


class SessionWriteContractError(AppError):
    """A caller tried to overwrite an existing session with a document it never read.

    The only dicts allowed to replace a stored row are ones that came out of
    ``read`` (they carry the version stamp) or brand-new sessions whose row does
    not exist yet. Anything else — a list projection, a hand-built dict, a copy
    that dropped the stamp — would replace the whole payload with whatever keys
    it happens to have.
    """

    def __init__(self, session_id: str) -> None:
        super().__init__(
            f"Refusing to overwrite session {session_id} with a document that was not read from the store.",
            status_code=500,
            retryable=False,
        )
        self.session_id = session_id
