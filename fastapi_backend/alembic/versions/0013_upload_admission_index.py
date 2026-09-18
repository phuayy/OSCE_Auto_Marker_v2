from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "idx_uploads_owner_status_expiry", "uploads", ["created_by", "status", "expires_at"], if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("idx_uploads_owner_status_expiry", table_name="uploads")
