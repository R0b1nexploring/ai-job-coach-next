"""Initial prototype schema.

Revision ID: 0001
Revises:
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "batches",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False, unique=True),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("jd_text", sa.Text(), nullable=False),
        sa.Column("material_text", sa.Text(), nullable=False),
        sa.Column("material_confirmed", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("error_code", sa.String(50)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("plans", sa.JSON()),
    )
    op.create_table(
        "resumes",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("batch_id", sa.String(32), sa.ForeignKey("batches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("batch_id", "kind"),
    )
    op.create_table(
        "guides",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("batch_id", sa.String(32), sa.ForeignKey("batches.id", ondelete="CASCADE"), nullable=False),
        sa.Column("resume_id", sa.String(32), sa.ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("sections", sa.JSON()),
        sa.Column("error_code", sa.String(50)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("batch_id", "kind"),
    )


def downgrade() -> None:
    op.drop_table("guides")
    op.drop_table("resumes")
    op.drop_table("batches")
