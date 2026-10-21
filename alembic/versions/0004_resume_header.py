"""Preserve manually entered resume headers in current and historical revisions."""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("resumes", "resume_revisions"):
        op.add_column(table, sa.Column("header", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))


def downgrade() -> None:
    for table in ("resume_revisions", "resumes"):
        op.drop_column(table, "header")
