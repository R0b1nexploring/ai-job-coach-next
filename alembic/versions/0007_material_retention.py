"""Distinguish batch-owned from explicitly reusable v2 materials."""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Existing 0006 values are unknown and must not be assumed independent.
    op.add_column("material_revisions", sa.Column("retention", sa.String(16)))
    op.add_column("material_revisions", sa.Column("owner_batch_id", sa.String(32)))


def downgrade() -> None:
    raise RuntimeError("0007 cannot safely downgrade material deletion ownership")
