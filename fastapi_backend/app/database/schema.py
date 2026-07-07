SCHEMA_VERSION = 1


SQLITE_SCHEMA_STATEMENTS = [
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
    """
    CREATE INDEX IF NOT EXISTS idx_jobs_status_created_at
    ON jobs(status, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_jobs_session_task_status
    ON jobs(session_id, task_type, status)
    """,
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
    """
    CREATE INDEX IF NOT EXISTS idx_job_attempts_job_id
    ON job_attempts(job_id, attempt_number)
    """,
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
    """
    CREATE INDEX IF NOT EXISTS idx_job_events_job_id
    ON job_events(job_id, created_at)
    """,
]


POSTGRES_SCHEMA_STATEMENTS = [
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
    """
    CREATE INDEX IF NOT EXISTS idx_jobs_status_created_at
    ON jobs(status, created_at)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_jobs_session_task_status
    ON jobs(session_id, task_type, status)
    """,
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
    """
    CREATE INDEX IF NOT EXISTS idx_job_attempts_job_id
    ON job_attempts(job_id, attempt_number)
    """,
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
    """
    CREATE INDEX IF NOT EXISTS idx_job_events_job_id
    ON job_events(job_id, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS app_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
]


SCHEMA_STATEMENTS = SQLITE_SCHEMA_STATEMENTS
