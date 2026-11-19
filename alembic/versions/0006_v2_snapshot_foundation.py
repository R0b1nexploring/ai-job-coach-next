"""Add v2 snapshot storage without rewriting any historical v1 data."""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "material_revisions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("material_id", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("extracted_text", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(255)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("material_id", "revision"),
    )
    op.create_table(
        "fact_snapshots",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("scope_id", sa.String(64), nullable=False),
        sa.Column("claims", sa.JSON(), nullable=False),
        sa.Column("source_copies", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in (
        sa.Column("schema_version", sa.Integer()),
        sa.Column("material_revision_ids", sa.JSON()),
        sa.Column("fact_snapshot_id", sa.String(32)),
        sa.Column("target_snapshot", sa.JSON()),
        sa.Column("generation_input_hash", sa.String(100)),
        sa.Column("resume_locale", sa.String(16)),
        sa.Column("guide_locale", sa.String(16)),
        sa.Column("generation_preferences", sa.JSON()),
    ):
        op.add_column("batches", column)
    for table in ("resumes", "resume_revisions"):
        op.add_column(table, sa.Column("schema_version", sa.Integer()))
        op.add_column(table, sa.Column("document_v2", sa.JSON()))
    for table in ("guides", "guide_revisions"):
        op.add_column(table, sa.Column("schema_version", sa.Integer()))
        op.add_column(table, sa.Column("guide_locale", sa.String(16)))
        op.add_column(table, sa.Column("guide_input_hash", sa.String(100)))
    op.add_column("guide_revisions", sa.Column("v2_snapshot", sa.JSON()))
    op.add_column("guide_revisions", sa.Column("coverage_unit_ids", sa.JSON()))


def downgrade() -> None:
    # New v2 data cannot be represented in v1 without losing information.
    raise RuntimeError("0006 cannot safely downgrade v2 snapshots; restore a pre-upgrade backup instead")
