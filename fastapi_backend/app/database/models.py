from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class RubricAsset(Base):
    __tablename__ = "rubric_assets"
    __table_args__ = (
        UniqueConstraint(
            "rubric_type",
            "normalized_name",
            "size_bytes",
            "content_sha256",
            name="uq_rubric_assets_identity",
        ),
        Index("idx_rubric_assets_hash", "content_sha256"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    rubric_type: Mapped[str] = mapped_column(String(40), nullable=False)
    original_name: Mapped[str] = mapped_column(String(512), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(512), nullable=False)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str | None] = mapped_column(String(255))
    storage_provider: Mapped[str] = mapped_column(String(64), nullable=False, default="local")
    storage_key: Mapped[str | None] = mapped_column(Text)
    absolute_path: Mapped[str] = mapped_column(Text, nullable=False)
    public_url: Mapped[str | None] = mapped_column(Text)
    storage_ref_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    times_used: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    last_used_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class StudentRecord(Base):
    __tablename__ = "students"
    __table_args__ = (UniqueConstraint("external_id", name="uq_students_external_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    assessments: Mapped[list["AssessmentSessionRecord"]] = relationship(back_populates="student")


class ExaminerRecord(Base):
    __tablename__ = "examiners"
    __table_args__ = (UniqueConstraint("external_id", name="uq_examiners_external_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    examiner_type: Mapped[str] = mapped_column(String(40), nullable=False, default="ai")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    results: Mapped[list["AssessmentResultRecord"]] = relationship(back_populates="examiner")


class AssessmentSessionRecord(Base):
    __tablename__ = "assessment_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    student_id: Mapped[str] = mapped_column(ForeignKey("students.id"), nullable=False)
    case_study_rubric_id: Mapped[str | None] = mapped_column(ForeignKey("rubric_assets.id"))
    communication_rubric_id: Mapped[str | None] = mapped_column(ForeignKey("rubric_assets.id"))
    parent_session_id: Mapped[str | None] = mapped_column(String(36))
    workflow: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    session_name: Mapped[str | None] = mapped_column(String(255))
    video_file_name: Mapped[str | None] = mapped_column(String(512))
    case_study_file_name: Mapped[str | None] = mapped_column(String(512))
    session_json_path: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    student: Mapped[StudentRecord] = relationship(back_populates="assessments")
    results: Mapped[list["AssessmentResultRecord"]] = relationship(
        back_populates="assessment_session",
        cascade="all, delete-orphan",
    )


class AssessmentResultRecord(Base):
    __tablename__ = "assessment_results"
    __table_args__ = (
        UniqueConstraint("assessment_session_id", "result_type", name="uq_assessment_results_type"),
        Index("idx_assessment_results_session", "assessment_session_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    assessment_session_id: Mapped[str] = mapped_column(ForeignKey("assessment_sessions.id"), nullable=False)
    examiner_id: Mapped[str] = mapped_column(ForeignKey("examiners.id"), nullable=False)
    result_type: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="completed")
    score_total: Mapped[float | None] = mapped_column(Float)
    score_max: Mapped[float | None] = mapped_column(Float)
    pass_fail: Mapped[str | None] = mapped_column(String(80))
    output_path: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    assessment_session: Mapped[AssessmentSessionRecord] = relationship(back_populates="results")
    examiner: Mapped[ExaminerRecord] = relationship(back_populates="results")
    criteria: Mapped[list["AssessmentCriterionRecord"]] = relationship(
        back_populates="result",
        cascade="all, delete-orphan",
    )


class AssessmentCriterionRecord(Base):
    __tablename__ = "assessment_criteria"
    __table_args__ = (Index("idx_assessment_criteria_result", "result_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    result_id: Mapped[str] = mapped_column(ForeignKey("assessment_results.id"), nullable=False)
    criterion_index: Mapped[int] = mapped_column(Integer, nullable=False)
    criterion_key: Mapped[str | None] = mapped_column(String(255))
    label: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float)
    max_score: Mapped[float | None] = mapped_column(Float)
    passed: Mapped[bool | None] = mapped_column(Boolean)
    is_critical: Mapped[bool | None] = mapped_column(Boolean)
    score_label: Mapped[str | None] = mapped_column(String(80))
    timestamp: Mapped[str | None] = mapped_column(String(40))
    evidence: Mapped[str | None] = mapped_column(Text)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    result: Mapped[AssessmentResultRecord] = relationship(back_populates="criteria")


class VideoRecord(Base):
    __tablename__ = "source_videos"
    __table_args__ = (Index("idx_source_videos_session_id", "session_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(36), nullable=False)
    original_name: Mapped[str] = mapped_column(String(512), nullable=False)
    safe_name: Mapped[str] = mapped_column(String(512), nullable=False)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(255), nullable=True)
    storage_provider: Mapped[str] = mapped_column(String(64), nullable=False, default="local")
    storage_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    absolute_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    public_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    storage_ref_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class NotificationRecord(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        Index("idx_notifications_created_at", "created_at"),
        Index("idx_notifications_read_at", "read_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Wire identifier from domain.notifications.NotificationType. Nullable, and
    # backfilled to a legacy default by the additive migration, because rows
    # written before webhooks existed have no type.
    event_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    # Null = unread. Single-user system, so read state lives on the row itself.
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WebhookSubscriptionRecord(Base):
    """An outbound HTTP endpoint that receives notification events.

    The secret is stored in plaintext because it is not a credential *for* this
    system — it is the shared key the subscriber uses to verify our HMAC
    signature, and we must be able to re-sign every delivery with it. It is
    never returned by the API after creation (only a masked preview is).
    """

    __tablename__ = "webhook_subscriptions"
    __table_args__ = (Index("idx_webhook_subscriptions_active", "active"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    description: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    url: Mapped[str] = mapped_column(Text, nullable=False)
    secret: Mapped[str] = mapped_column(String(128), nullable=False)
    # Event types this endpoint wants. Empty list or ["*"] = every type.
    event_types: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    # Denormalised summary of the most recent delivery, so the management UI can
    # show endpoint health without joining the (capped) delivery log.
    last_delivery_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class WebhookDeliveryRecord(Base):
    """One delivery attempt of one event to one subscription.

    Kept for operator debugging ("did my endpoint get it, and what did it say?").
    Pruned to a bounded number of rows per subscription — this is a log, not an
    audit trail, and it must never grow without limit.
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        Index("idx_webhook_deliveries_subscription", "subscription_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    subscription_id: Mapped[str] = mapped_column(String(36), nullable=False)
    notification_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # "succeeded" | "failed"
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class CorpusRecord(Base):
    """Named list of domain terms used to bias WhisperX (--hotwords) and drive
    deterministic transcript correction. Picked per session at upload time; the
    chosen terms are snapshotted into the session payload, so editing or
    deleting a corpus never affects existing sessions."""

    __tablename__ = "corpora"
    __table_args__ = (UniqueConstraint("name", name="uq_corpora_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    terms: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class AppSettingRecord(Base):
    """Global application settings as key/value rows. The pipeline reads them
    live from the shared DB at run time, so a change applies to every
    subsequent run — including clip children and the Hatchet worker process —
    without a restart or per-session snapshot."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class ProviderCredentialRecord(Base):
    """One scoring provider's API key, sealed with this deployment's master key.

    Deliberately not a row in ``app_settings``. That table is returned verbatim
    by ``GET /api/settings`` and is the screen's own state; a credential put
    there would be readable by anyone who can open settings and would ride along
    in every database backup as plaintext. This table is never serialised to a
    client: the API answers with ``last4`` and timestamps only.

    ``key_fingerprint`` identifies the master key that sealed ``ciphertext``, so
    a deployment whose key was rotated or lost reports "re-enter this key"
    rather than decrypting to nonsense. ``nonce`` is per-write, never reused.
    """

    __tablename__ = "provider_credentials"

    provider_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ciphertext: Mapped[str] = mapped_column(Text, nullable=False)
    nonce: Mapped[str] = mapped_column(String(64), nullable=False)
    key_fingerprint: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    # Last four characters of the key, so an operator can tell which credential
    # is installed without the server ever handing the credential back.
    last4: Mapped[str] = mapped_column(String(8), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Outcome of the last connection test run against this key. Kept on the row
    # so the settings screen can show "verified 10 minutes ago" after a reload
    # instead of forgetting every probe the moment the page is closed.
    last_tested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_test_ok: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    last_test_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class SessionRecord(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        Index("idx_sessions_status", "status"),
        Index("idx_sessions_parent_session_id", "parent_session_id"),
        Index("idx_sessions_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="uploaded")
    parent_session_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # Stores the clip provenance object {"clipId": ..., "label": ...} for child
    # sessions created from exported clips; null for top-level sessions. Declared
    # as JSON because the application model is a structured object, not a scalar.
    clip_source: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)


class TableVersionRecord(Base):
    """Monotonic change counter per tracked table, bumped by a database trigger.

    The API caches expensive projections (notably the session index) and needs a
    *cheap* way to ask "has anything changed since I built this?". Reading one
    small row per tracked table answers that in a single indexed query, instead
    of re-reading and re-deserialising every ``sessions.payload`` JSON document
    on each poll.

    The counter lives in the database rather than in process memory because the
    Hatchet worker writes sessions from a *separate process* — an in-process
    cache would go stale with no way to learn about it. A trigger-maintained row
    is visible to every process that shares the database.
    """

    __tablename__ = "table_versions"

    table_name: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
