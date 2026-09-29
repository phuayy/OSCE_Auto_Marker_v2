"""The one place that decides what a configured database URL/path means.

``APP_DATABASE_URL`` / ``DATABASE_URL`` reach five different consumers —
``OrmDatabase`` (the app itself), ``alembic/env.py``, ``scripts/deploy_check.py``,
``scripts/backup_database.py`` and ``scripts/check_database.py`` /
``scripts/reset_db.py`` — and every one of them used to normalise it a little
differently (or not validate it at all). That drift is exactly how a typo'd
or unsupported scheme (``postgresql+asyncpg://``, a stray space, ``mysql://``)
used to silently pass every check: whichever consumer hit it first either fell
through to treating the *entire URL string* as a SQLite file path (nothing
before this module validated at all) or raised an unrelated, unhelpful error
(``Settings.resolved_database_path``'s own message, which named no scheme and
carried no hint). The database that was actually configured stayed untouched;
a fresh, empty local SQLite file quietly took its place, or the operator saw
a confusing crash that gave them nothing to fix.

Two things make this module the single source of truth:

* :func:`looks_like_a_url` is the one test that decides "is this a URL I must
  validate the scheme of, or a filesystem path". It is keyed on the literal
  ``"://"`` substring, not on ``urlparse().scheme`` — ``urlparse`` happily
  reports ``"c"`` as the scheme for ``C:\\data\\osce.sqlite3`` *and*
  ``C:/data/osce.sqlite3`` (a single colon-slash, not ``"://"``), which would
  otherwise misroute every Windows path through scheme validation instead of
  treating it as the SQLite file path it is.
* :func:`normalize_database_url` is what every consumer above now calls
  (directly, or through ``OrmDatabase._normalize_url``, which is now a thin
  wrapper around it). A path — a ``Path`` object, a relative path, an
  absolute POSIX path, a ``~`` path, or a Windows path in either slash style —
  is always SQLite. A string containing ``"://"`` MUST use one of
  :data:`SUPPORTED_SCHEMES`; anything else raises :class:`DatabaseUrlError`
  with the scheme it saw (redacted — see :func:`redact_database_url`), the
  schemes this app accepts, and, for a handful of known near-misses (a
  different Postgres/SQLite driver spelling), the corrected scheme to use
  instead. There is no fallback: an unrecognised URL is a configuration
  error, in every environment, not a path to guess at.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit

# Every scheme this app can actually connect through. Kept as two named sets
# (rather than one flat tuple) because callers such as
# Settings.resolved_database_source need to tell "this is Postgres, return it
# unchanged" apart from "this is SQLite, resolve it to a Path" without
# re-deriving the split themselves.
POSTGRES_SCHEMES = frozenset({"postgres", "postgresql", "postgresql+psycopg"})
SQLITE_SCHEMES = frozenset({"sqlite", "sqlite+aiosqlite"})
SUPPORTED_SCHEMES = tuple(sorted(POSTGRES_SCHEMES | SQLITE_SCHEMES))

# Schemes someone plausibly typed that this app cannot use, mapped to the
# correct spelling. Not exhaustive — a scheme not in here still fails, just
# without a specific pointer to the fix — but these are the ones a working
# SQLAlchemy install elsewhere on the machine, an old tutorial, or muscle
# memory from another project most commonly produces. This app installs
# psycopg (v3), never asyncpg or psycopg2; SQLAlchemy's own sync sqlite3
# driver is spelled "sqlite" or "sqlite+pysqlite", never used here because
# every path in this app goes through the async "sqlite+aiosqlite" engine.
_KNOWN_WRONG_SCHEMES: dict[str, str] = {
    "postgresql+asyncpg": "postgresql+psycopg",
    "postgres+asyncpg": "postgresql+psycopg",
    "postgresql+psycopg2": "postgresql+psycopg",
    "postgres+psycopg2": "postgresql+psycopg",
    "postgresql+pg8000": "postgresql+psycopg",
    "postgres+pg8000": "postgresql+psycopg",
    "sqlite+pysqlite": "sqlite+aiosqlite",
    "sqlite+pysqlite3": "sqlite+aiosqlite",
}


class DatabaseUrlError(ValueError):
    """A configured database URL uses a scheme this app cannot connect through.

    A ``ValueError`` subclass on purpose: every existing ``except ValueError``
    around a call that used to raise the bare ``ValueError`` here (tests,
    ``scripts/deploy_check.py``'s ``DeployCheckError`` wrapping) keeps
    catching it without change.
    """


def redact_database_url(url: str) -> str:
    """Replace a URL's password with ``***``, leaving everything else as-is.

    The one copy of this logic — previously duplicated in
    ``scripts/check_database.py`` (which now imports it from here). Used by
    :func:`normalize_database_url` so a rejected URL's error message can
    safely name what it saw without ever printing a credential to a log, a
    console, or (for ``scripts/deploy_check.py --json``) a JSON payload
    another process might persist.
    """
    parsed = urlsplit(url)
    if parsed.password:
        username = parsed.username or ""
        hostname = parsed.hostname or ""
        passwordless_auth = f"{username}:***@" if username else ""
        port = f":{parsed.port}" if parsed.port else ""
        netloc = f"{passwordless_auth}{hostname}{port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
    # urlsplit found no password structurally — either there genuinely is
    # none, or (a malformed scheme containing a space, exactly the kind of
    # typo this module's own error messages need to quote) urlsplit could
    # not parse the string as a URL at all, so parsed.username/.password/
    # .hostname are all empty and the credential is still sitting in
    # parsed.path verbatim. A plain regex over the raw string is what
    # catches that case: a credential must never reach an error message
    # just because the URL around it happens to be broken too.
    return re.sub(r"://([^/@:\s]*):([^@/\s]+)@", r"://\1:***@", url)


def looks_like_a_url(raw: str) -> bool:
    """True for a string that must be scheme-validated as a URL; false for a
    filesystem path.

    Keyed on the literal ``"://"`` substring rather than ``urlparse().scheme``
    — see the module docstring for why a Windows path in either slash style
    would otherwise be misrouted through scheme validation (``urlparse``
    reports a bogus scheme of ``"c"`` for both ``C:\\...`` and ``C:/...``, a
    single colon-slash rather than the colon-slash-slash a real URL has).
    """
    return "://" in raw


def normalize_database_url(database_url_or_path: Path | str) -> str:
    """The one normalisation every consumer of the configured database URL
    uses: ``OrmDatabase``, ``alembic/env.py``, ``scripts/deploy_check.py``,
    ``scripts/backup_database.py``, ``scripts/check_database.py`` and
    ``scripts/reset_db.py``.

    A ``Path``, or a string with no ``"://"``, is always a SQLite file path —
    this covers a relative path, an absolute POSIX path, a ``~`` path, and a
    Windows path in either slash style. A string containing ``"://"`` is a
    URL and MUST use one of :data:`SUPPORTED_SCHEMES`; anything else raises
    :class:`DatabaseUrlError`. Never returns a fallback for an unrecognised
    URL — that silent SQLite-file-path reinterpretation is the exact bug this
    module exists to close.
    """
    if isinstance(database_url_or_path, Path):
        return _sqlite_url(database_url_or_path)

    raw = str(database_url_or_path).strip()
    if not raw:
        raise DatabaseUrlError("Database URL must not be empty.")

    if not looks_like_a_url(raw):
        return _sqlite_url(Path(raw).expanduser())

    parsed = urlparse(raw)
    scheme = parsed.scheme

    if scheme in POSTGRES_SCHEMES:
        if scheme == "postgresql+psycopg":
            return raw
        # "postgres://..." / "postgresql://..." -> the +psycopg-qualified
        # form SQLAlchemy's async engine needs. Exact behaviour preserved
        # from before this module existed (a scheme-string replace, not a
        # full re-serialisation, so query params/case are untouched).
        return raw.replace(f"{scheme}://", "postgresql+psycopg://", 1)

    if scheme in SQLITE_SCHEMES:
        if scheme == "sqlite+aiosqlite":
            return raw
        return raw.replace("sqlite://", "sqlite+aiosqlite://", 1)

    redacted = redact_database_url(raw)
    scheme_label = scheme if scheme else "(no scheme recognised)"
    hint = ""
    if scheme in _KNOWN_WRONG_SCHEMES:
        hint = f" Use {_KNOWN_WRONG_SCHEMES[scheme]}:// instead."
    raise DatabaseUrlError(
        f"Unsupported database URL scheme {scheme_label!r} in {redacted!r}. "
        f"Supported schemes: {', '.join(SUPPORTED_SCHEMES)}.{hint}"
    )


def _sqlite_url(path: Path) -> str:
    resolved = path.expanduser().resolve()
    return f"sqlite+aiosqlite:///{resolved.as_posix()}"
