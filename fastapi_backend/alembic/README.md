# Database migrations

Alembic owns the schema for both database layers: the SQLAlchemy ORM tables
(`sessions`, `assessments`, `rubric_assets`, `notifications`, …) and the raw-SQL
jobs tables (`jobs`, `job_attempts`, `job_events`, `app_metadata`). It also
installs the change-tracking triggers that maintain `table_versions`.

All commands run from `fastapi_backend/`, which is where `alembic.ini` lives and
where `app` is importable.

```bash
cd fastapi_backend

alembic upgrade head            # apply everything pending
alembic current                 # what this database is stamped at
alembic history --verbose       # the revision tree
alembic downgrade -1            # step back one revision
alembic upgrade head --sql      # print the SQL instead of running it
```

The database URL is **not** in `alembic.ini`. `env.py` resolves it from the
application's own `Settings` — `APP_DATABASE_URL`, then `DATABASE_URL`, then the
default SQLite file under `storage/` — so the CLI and the running app can never
disagree about which database they mean. For a one-off run against a different
database:

```bash
alembic -x db_url=postgresql+psycopg://user:pass@host/db upgrade head
```

## Revisions

| Revision | What it does |
|---|---|
| `0001_initial_schema` | Baseline: every ORM table plus the raw-SQL jobs tables. Mirrors the schema the old `create_all` startup path produced, minus `notifications.event_type`. |
| `0002_notification_event_type` | Adds `notifications.event_type` and backfills legacy rows to `scoring.completed`. Mirrors the `ADDITIVE_MIGRATIONS` entry in `app/database/migrations.py`. |
| `0003_change_tracking_triggers` | Installs the per-table triggers that bump `table_versions.version` (and `pg_notify` on PostgreSQL). Mirrors `install_change_tracking()`. |

Revision files are self-contained snapshots — they never import
`app.database.models` or `app.database.schema`. A migration has to keep
describing the schema as it was when it was written; importing live application
code would silently rewrite history every time a model changes. When the models
and a migration drift apart, the fix is a **new** revision, not an edit to an
old one.

## Startup behaviour

`AppContainer.startup()` runs `alembic upgrade head` before anything touches the
database, so `python scripts/run_api.py` still brings the app up on a fresh
machine in one command. Three cases, decided by what the database already
contains:

- **`alembic_version` present** — upgrade to head.
- **application tables present, no `alembic_version`** — a database built by the
  old `create_all` path. It is stamped at `0001` (the baseline describing what it
  already has) and *then* upgraded, so post-baseline revisions still get applied.
  Stamping straight at `head` is how a pre-Alembic database ends up permanently
  missing a column.
- **empty** — upgrade from scratch.

Set `DB_AUTO_MIGRATE=false` to turn the startup pass off and run
`alembic upgrade head` as a deliberate deploy step instead. That is the right
setting when several processes boot at once (API plus a Hatchet worker) and only
one of them should be touching the schema.

The pre-Alembic startup path — `create_all`, `apply_additive_migrations()`,
`install_change_tracking()` — is still in place and still runs. On a migrated
database each of those finds nothing to do; they remain as the fallback for when
`DB_AUTO_MIGRATE` is off or Alembic is not installed.

## Adding a revision

```bash
cd fastapi_backend
alembic revision --autogenerate -m "add whatever"
```

Autogenerate compares `Base.metadata` against the live database and ignores the
raw-SQL jobs tables (they are not in the metadata, so `env.py` filters them out
rather than proposing to drop them). **Read the generated file before committing
it** — autogenerate does not detect renames, server-side default changes, or
anything expressed outside the metadata, and on SQLite it needs the batch mode
`env.py` already enables.

Name the file with the next number in sequence (`0004_…`) and set `revision` to
that same number, matching the existing files.
