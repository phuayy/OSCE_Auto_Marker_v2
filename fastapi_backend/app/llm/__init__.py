"""Provider-agnostic LLM access for the OSCE marker.

Every model call in this project — content scoring, communication scoring and
transcript preprocessing — used to construct an ``OpenAI`` client against a
hard-coded NVIDIA base URL. That made the vendor a build-time decision: when a
provider had an outage or deprecated a checkpoint, the only remedy was editing
three scripts.

This package makes the vendor a *runtime* decision. A provider is a module plus
a registry entry; the operator picks a primary and a fallback model in the
settings screen; the router runs the primary, degrades its request shape when a
provider rejects a feature, retries transient failures with jittered backoff,
trips a circuit breaker on a provider that keeps failing, and moves to the
fallback rather than losing the run.

The package is deliberately synchronous and free of FastAPI/SQLAlchemy imports:
the scoring work happens in subprocesses (``scripts/``), and those import this
same code so the API and the scorers can never disagree about routing.
"""
