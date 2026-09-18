from alembic import context, op
import sqlalchemy as sa

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not context.is_offline_mode() and sa.inspect(op.get_bind()).has_table("uploads"):
        indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("sessions")}
        if "idx_sessions_created_id" not in indexes:
            op.create_index("idx_sessions_created_id", "sessions", ["created_at", "id"])
        return
    op.create_table(
        "uploads",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("session_id", sa.String(36), sa.ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("files_json", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.String(36)),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("idx_uploads_session", "uploads", ["session_id"])
    op.create_index("idx_uploads_expiry", "uploads", ["expires_at", "status"])
    op.create_index("idx_sessions_created_id", "sessions", ["created_at", "id"])


def downgrade() -> None:
    op.drop_index("idx_sessions_created_id", table_name="sessions")
    op.drop_table("uploads")
