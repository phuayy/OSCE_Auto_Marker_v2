from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
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
