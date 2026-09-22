"""Which settings keys are per-user, and which stay deployment-wide.

Eight keys move with the account: which transcription engine it runs, how it
marks content (one model or a panel, and which), whether the transcript
preprocessing pass runs before scoring, and whether the screens show a score
as a percentage or as raw points. Everything else — which providers this
deployment can reach at all and the API keys that authorise them, the
transcription corpus, the communication rubric, webhooks, and any setting
added later — is one shared answer for everyone, an admin's to set.

The literal key strings are repeated here rather than imported from
``app.repositories.app_settings_repository`` — domain modules take no
dependency on a repository, the same rule ``app.domain.sessions`` and
``app.domain.users`` already follow. ``tests/test_settings_scope.py`` pins the
two spellings against each other so they cannot drift apart.

The rule this module exists to enforce is fail-safe: a key not named in
``USER_SCOPED_KEYS`` is deployment-scoped, so a setting added later is
private-to-the-operator by default rather than silently becoming everyone's
to change.
"""

from __future__ import annotations

USER_SCOPED_KEYS: frozenset[str] = frozenset(
    {
        "transcriptionEngine",
        "transcriptionEngineOptions",
        "llmPrimary",
        "llmFallbacks",
        "llmMarkingMode",
        "llmPanel",
        "llmTranscriptPreprocess",
        "scoreDisplay",
    }
)


def is_user_scoped(key: str) -> bool:
    """Whether ``key`` is a marker's own preference rather than deployment
    config. False — deployment-scoped, admin-only — is the default for
    anything this module does not explicitly name."""
    return key in USER_SCOPED_KEYS
