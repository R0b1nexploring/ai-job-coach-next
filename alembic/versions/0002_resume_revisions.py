"""Add immutable resume history and optimistic revision."""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("resumes", sa.Column("revision", sa.Integer(), nullable=False, server_default="1"))
    op.create_table(
        "resume_revisions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("resume_id", sa.String(32), sa.ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(160), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("items", sa.JSON(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("resume_id", "revision"),
    )
    op.execute(sa.text("INSERT INTO resume_revisions (id, resume_id, revision, title, summary, items, content_hash, created_at) SELECT lower(hex(randomblob(16))), id, 1, title, summary, items, content_hash, created_at FROM resumes"))


def downgrade() -> None:
    op.drop_table("resume_revisions")
    op.drop_column("resumes", "revision")
