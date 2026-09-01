"""Baseline schema: ORM tables plus the raw-SQL jobs tables.

Revision ID: 0001
Revises:
Create Date: 2026-09-01

This is a *snapshot*, deliberately self-contained: it never imports
``app.database.models`` or ``app.database.schema``. A migration has to keep
describing the schema as it was when it was written, and importing live
application code would silently rewrite history every time a model changes.

The ORM half mirrors ``Base.metadata`` at the point Alembic was introduced,
minus ``notifications.event_type`` -- that column arrived later and is added by
revision 0002, so a database that predates it walks the same path as a fresh
one.

The jobs half is copied verbatim from ``app/database/schema.py``, which the
raw-aiosqlite layer still runs with ``CREATE TABLE IF NOT EXISTS`` at startup.
Creating them here means a database built purely by ``alembic upgrade head``
is complete; the startup pass then finds nothing to do.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


# Postgres wants BIGSERIAL where SQLite wants INTEGER ... AUTOINCREMENT, so the
# jobs DDL is dialect-specific. Everything else is identical between the two.
_JOBS_TABLES_SQLITE = (
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        task_type TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        payload_json TEXT NOT NULL DEFAULT '{}',
        error TEXT,
        created_at TEXT NOT NULL,
        queued_at TEXT,
        started_at TEXT,
        ended_at TEXT,
        requeued_at TEXT,
        locked_by TEXT,
        locked_at TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_status_created_at ON jobs(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_session_task_status ON jobs(session_id, task_type, status)",
    """
    CREATE TABLE IF NOT EXISTS job_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL,
        attempt_number INTEGER NOT NULL,
        worker_id TEXT,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        error TEXT,
        FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_job_attempts_job_id ON job_attempts(job_id, attempt_number)",
    """
    CREATE TABLE IF NOT EXISTS job_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        message TEXT,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_job_events_job_id ON job_events(job_id, created_at)",
)

_JOBS_TABLES_POSTGRES = (
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        task_type TEXT NOT NULL,
        status TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 3,
        payload_json TEXT NOT NULL DEFAULT '{}',
        error TEXT,
        created_at TEXT NOT NULL,
        queued_at TEXT,
        started_at TEXT,
        ended_at TEXT,
        requeued_at TEXT,
        locked_by TEXT,
        locked_at TEXT,
        updated_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_status_created_at ON jobs(status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_session_task_status ON jobs(session_id, task_type, status)",
    """
    CREATE TABLE IF NOT EXISTS job_attempts (
        id BIGSERIAL PRIMARY KEY,
        job_id TEXT NOT NULL,
        attempt_number INTEGER NOT NULL,
        worker_id TEXT,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        error TEXT,
        FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_job_attempts_job_id ON job_attempts(job_id, attempt_number)",
    """
    CREATE TABLE IF NOT EXISTS job_events (
        id BIGSERIAL PRIMARY KEY,
        job_id TEXT NOT NULL,
        event_type TEXT NOT NULL,
        message TEXT,
        payload_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_job_events_job_id ON job_events(job_id, created_at)",
    """
    CREATE TABLE IF NOT EXISTS app_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
)

_DROP_JOBS_TABLES = (
    "DROP TABLE IF EXISTS job_events",
    "DROP TABLE IF EXISTS job_attempts",
    "DROP TABLE IF EXISTS jobs",
    "DROP TABLE IF EXISTS app_metadata",
)


def _timestamp(name: str, *, nullable: bool = False) -> sa.Column:
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def upgrade() -> None:
    op.create_table(
        "rubric_assets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("rubric_type", sa.String(40), nullable=False),
        sa.Column("original_name", sa.String(512), nullable=False),
        sa.Column("normalized_name", sa.String(512), nullable=False),
        sa.Column("file_name", sa.String(512), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("mime_type", sa.String(255), nullable=True),
        sa.Column("storage_provider", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("absolute_path", sa.Text(), nullable=False),
        sa.Column("public_url", sa.Text(), nullable=True),
        sa.Column("storage_ref_json", sa.JSON(), nullable=False),
        sa.Column("times_used", sa.Integer(), nullable=False),
        _timestamp("created_at"),
        _timestamp("last_used_at"),
        sa.UniqueConstraint(
            "rubric_type",
            "normalized_name",
            "size_bytes",
            "content_sha256",
            name="uq_rubric_assets_identity",
        ),
    )
    op.create_index("idx_rubric_assets_hash", "rubric_assets", ["content_sha256"])

    op.create_table(
        "students",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.UniqueConstraint("external_id", name="uq_students_external_id"),
    )

    op.create_table(
        "examiners",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("examiner_type", sa.String(40), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.UniqueConstraint("external_id", name="uq_examiners_external_id"),
    )

    op.create_table(
        "assessment_sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("student_id", sa.String(36), sa.ForeignKey("students.id"), nullable=False),
        sa.Column(
            "case_study_rubric_id",
            sa.String(36),
            sa.ForeignKey("rubric_assets.id"),
            nullable=True,
        ),
        sa.Column(
            "communication_rubric_id",
            sa.String(36),
            sa.ForeignKey("rubric_assets.id"),
            nullable=True,
        ),
        sa.Column("parent_session_id", sa.String(36), nullable=True),
        sa.Column("workflow", sa.String(40), nullable=True),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("session_name", sa.String(255), nullable=True),
        sa.Column("video_file_name", sa.String(512), nullable=True),
        sa.Column("case_study_file_name", sa.String(512), nullable=True),
        sa.Column("session_json_path", sa.Text(), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
    )

    op.create_table(
        "assessment_results",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "assessment_session_id",
            sa.String(36),
            sa.ForeignKey("assessment_sessions.id"),
            nullable=False,
        ),
        sa.Column("examiner_id", sa.String(36), sa.ForeignKey("examiners.id"), nullable=False),
        sa.Column("result_type", sa.String(40), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("score_total", sa.Float(), nullable=True),
        sa.Column("score_max", sa.Float(), nullable=True),
        sa.Column("pass_fail", sa.String(80), nullable=True),
        sa.Column("output_path", sa.Text(), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.UniqueConstraint(
            "assessment_session_id", "result_type", name="uq_assessment_results_type"
        ),
    )
    op.create_index(
        "idx_assessment_results_session", "assessment_results", ["assessment_session_id"]
    )

    op.create_table(
        "assessment_criteria",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "result_id", sa.String(36), sa.ForeignKey("assessment_results.id"), nullable=False
        ),
        sa.Column("criterion_index", sa.Integer(), nullable=False),
        sa.Column("criterion_key", sa.String(255), nullable=True),
        sa.Column("label", sa.Text(), nullable=True),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("max_score", sa.Float(), nullable=True),
        sa.Column("passed", sa.Boolean(), nullable=True),
        sa.Column("is_critical", sa.Boolean(), nullable=True),
        sa.Column("score_label", sa.String(80), nullable=True),
        sa.Column("timestamp", sa.String(40), nullable=True),
        sa.Column("evidence", sa.Text(), nullable=True),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        _timestamp("created_at"),
    )
    op.create_index("idx_assessment_criteria_result", "assessment_criteria", ["result_id"])

    op.create_table(
        "source_videos",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), nullable=False),
        sa.Column("original_name", sa.String(512), nullable=False),
        sa.Column("safe_name", sa.String(512), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("mime_type", sa.String(255), nullable=True),
        sa.Column("storage_provider", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("absolute_path", sa.Text(), nullable=True),
        sa.Column("public_url", sa.Text(), nullable=True),
        sa.Column("storage_ref_json", sa.JSON(), nullable=False),
        _timestamp("created_at"),
    )
    op.create_index("idx_source_videos_session_id", "source_videos", ["session_id"])

    # ``event_type`` is intentionally absent here; revision 0002 adds it.
    op.create_table(
        "notifications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        _timestamp("created_at"),
        _timestamp("read_at", nullable=True),
    )
    op.create_index("idx_notifications_created_at", "notifications", ["created_at"])
    op.create_index("idx_notifications_read_at", "notifications", ["read_at"])

    op.create_table(
        "webhook_subscriptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("description", sa.String(255), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("secret", sa.String(128), nullable=False),
        sa.Column("event_types", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        _timestamp("last_delivery_at", nullable=True),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), nullable=False),
    )
    op.create_index("idx_webhook_subscriptions_active", "webhook_subscriptions", ["active"])

    op.create_table(
        "webhook_deliveries",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("subscription_id", sa.String(36), nullable=False),
        sa.Column("notification_id", sa.String(36), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("status_code", sa.Integer(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        _timestamp("created_at"),
    )
    op.create_index(
        "idx_webhook_deliveries_subscription",
        "webhook_deliveries",
        ["subscription_id", "created_at"],
    )

    op.create_table(
        "corpora",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("terms", sa.JSON(), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.UniqueConstraint("name", name="uq_corpora_name"),
    )

    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(80), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
        _timestamp("updated_at"),
    )

    op.create_table(
        "sessions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(512), nullable=True),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("parent_session_id", sa.String(36), nullable=True),
        sa.Column("clip_source", sa.JSON(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
    )
    op.create_index("idx_sessions_status", "sessions", ["status"])
    op.create_index("idx_sessions_parent_session_id", "sessions", ["parent_session_id"])
    op.create_index("idx_sessions_created_at", "sessions", ["created_at"])

    # The change counter the API's cache invalidation reads. Populated by the
    # triggers installed in revision 0003.
    op.create_table(
        "table_versions",
        sa.Column("table_name", sa.String(64), primary_key=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
        _timestamp("updated_at"),
    )

    _create_jobs_tables()


def _create_jobs_tables() -> None:
    bind = op.get_bind()
    statements = (
        _JOBS_TABLES_POSTGRES if bind.dialect.name == "postgresql" else _JOBS_TABLES_SQLITE
    )
    for statement in statements:
        op.execute(sa.text(statement))

    if bind.dialect.name == "postgresql":
        # Mirrors what Database._initialize_postgres_sync() records, so the raw
        # layer's own version marker is present on an alembic-built database.
        op.execute(
            sa.text(
                "INSERT INTO app_metadata (key, value, updated_at) "
                "VALUES ('schema_version', '1', CURRENT_TIMESTAMP) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = CURRENT_TIMESTAMP"
            )
        )
    else:
        op.execute(sa.text("PRAGMA user_version = 1"))


def downgrade() -> None:
    for statement in _DROP_JOBS_TABLES:
        op.execute(sa.text(statement))

    op.drop_table("table_versions")
    op.drop_table("sessions")
    op.drop_table("app_settings")
    op.drop_table("corpora")
    op.drop_table("webhook_deliveries")
    op.drop_table("webhook_subscriptions")
    op.drop_table("notifications")
    op.drop_table("source_videos")
    op.drop_table("assessment_criteria")
    op.drop_table("assessment_results")
    op.drop_table("assessment_sessions")
    op.drop_table("examiners")
    op.drop_table("students")
    op.drop_table("rubric_assets")
