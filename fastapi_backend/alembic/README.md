# Database migrations

Alembic owns the schema, and `app/database/models.py` is the only description of
it — sessions, assessments, rubric assets, notifications and the job queue
(`jobs`, `job_attempts`, `job_events`) alike. It also installs the
change-tracking triggers that maintain `table_versions`.

Until revision `0007` the queue's tables were the exception: raw `CREATE TABLE`
strings in `app/database/schema.py`, run at startup over a second connection
pool. Autogenerate was told to ignore them, so `alembic check` could not see
drift in that half — and there was some (see `0007`). The list of tables still
outside the metadata is now two entries long and lives in
`app/database/schema_ownership.py`.

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
| `0001_initial_schema` | Baseline: every ORM table plus the jobs tables, which at the time were owned by the raw-SQL layer. Mirrors the schema the old `create_all` startup path produced, minus `notifications.event_type`. |
| `0002_notification_event_type` | Adds `notifications.event_type` and backfills legacy rows to `scoring.completed`. The compatibility bootstrap delegates here; there is no additive DDL registry. |
| `0003_change_tracking_triggers` | Installs the per-table triggers that bump `table_versions.version` (and `pg_notify` on PostgreSQL). Runtime validates these triggers rather than installing them. |
| `0004_provider_credentials` | Adds `provider_credentials`: the operator-managed LLM API keys, stored as AES-256-GCM ciphertext bound to their provider id. Never in `app_settings`, which is returned verbatim by `GET /api/settings`. |
| `0005_cache_invalidation_triggers` | Attaches change tracking to `provider_credentials` and `app_settings`. Both are resolved before every scoring run in every process and written a few times a year, so both are cached; these triggers are what evict those caches when a key is rotated or a model is switched. |
| `0007_jobs_tables_in_orm_metadata` | Folds `jobs`, `job_attempts` and `job_events` into the ORM metadata and makes their primary keys `NOT NULL` — SQLite implies that only for an `INTEGER PRIMARY KEY`, so `jobs.id` (TEXT) could hold a null under the hand-written DDL. A no-op on PostgreSQL. |
| `0009_sessions_created_by` | Adds `sessions.created_by` (nullable, indexed) — the creator's user id, mirrored out of the `createdBy` snapshot in the payload so "sessions by this account" is an indexed query. No backfill: earlier rows genuinely have no creator. Legacy adoption adds the index even if an older additive pass already added the column. |
| `0008_users_and_action_tokens` | Adds `users` (accounts: role, status, bcrypt hash, `token_version`) and `user_action_tokens` (the emailed invitation and password-reset links, stored as SHA-256), and attaches change tracking to `users` because every authenticated request checks the account row through a per-process cache. The first administrator is seeded at startup, not here. |
| `0006_custom_llm_providers` | Adds `llm_providers`: scoring providers an operator defines at runtime (endpoint, auth placement, versions, extra headers/query/body) instead of ones the build ships. No API key column - the credential goes to `provider_credentials` like every other provider's. Change tracking is attached in the same revision, because the catalogue is cached in every process. |
| `0010_sqlite_wal_and_fk_pragmas` | Pins the SQLite connection policy (WAL, NORMAL, foreign keys) and restores every change-tracking trigger, including the jobs triggers lost to the `0007` table rebuild. Trigger installation failures are fatal. PostgreSQL retains native durability and FK enforcement. |

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

Alembic is the only schema writer. `OrmDatabase.initialize()` uses the same
migration runner, including for in-memory SQLite. `apply_additive_migrations()`
is only a compatibility wrapper for legacy adoption and upgrade; there is no
column registry or `create_all` fallback. Alembic is a required dependency.

Workers and APIs with `DB_AUTO_MIGRATE=false` validate that the database is at
head and that change tracking is complete, without issuing DDL. Migrate before
starting them. Startup fails if the counter table or a required trigger is
missing or a PostgreSQL trigger is disabled. `install_change_tracking()` is a
compatibility name for that read-only validation. Readiness repeats the check
and returns 503 with `checks.changeTracking=false` on failure. There is no
silent tracking opt-out: polling an unchanged counter cannot keep account or
credential caches safe. Recover missing triggers using a reviewed repair
migration or a complete schema restore, not by stamping head.

### SQLite connection policy

Both runtime and online Alembic connections enable `journal_mode=WAL`,
`synchronous=NORMAL`, and `foreign_keys=ON`. WAL is persistent; the other two
settings are reapplied on every connection, including pool replacements.
In-memory databases correctly keep `journal_mode=memory`. The existing 30-second
busy timeout remains: WAL allows readers during writes, not concurrent writers.
Use a local filesystem with shared-memory locking, not a network-mounted WAL
database. NORMAL can lose the latest commits on power loss; database consistency
is preserved. Use SQLite's backup API or a quiesced/checkpointed backup; copying
only the main file while writers run can omit committed WAL data.

During online SQLite migrations only, FK enforcement is temporarily disabled
outside the transaction so batch table rebuilds do not cascade-delete children.
The runner checks `foreign_key_check` before and after the transaction and
restores enforcement on success or failure. Existing orphans stop the migration
without deleting data; resolve them deliberately before retrying. Revision 0010
does not attempt to set connection-local pragmas from inside a transaction.
Downgrading 0010 keeps the connection policy and repaired triggers intact.

Migration regression tests run with `uv run --no-sync pytest
tests/test_alembic_migrations.py tests/test_migrations.py` from this directory.
The optional PostgreSQL parity test uses `OSCE_TEST_POSTGRES_URL`, which must
point to a disposable database with permission to create schemas. It creates
uniquely named test schemas and leaves them in that disposable database. Both
paths are compared using columns, indexes and foreign keys; PostgreSQL column
comparison reads `pg_catalog` directly.

## Adding a revision

```bash
cd fastapi_backend
alembic revision --autogenerate -m "add whatever"
```

Autogenerate compares `Base.metadata` against the live database, ignoring only
the tables named in `app/database/schema_ownership.py`. **Read the generated file
before committing it** — autogenerate does not detect renames, server-side default changes, or
anything expressed outside the metadata, and on SQLite it needs the batch mode
`env.py` already enables.

Name the file with the next number in sequence (`0004_…`) and set `revision` to
that same number, matching the existing files.
