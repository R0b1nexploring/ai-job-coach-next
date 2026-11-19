"""Reconcile both deployed 0004 schemas and preserve independent headers.

The original next checkout used 0004 for resume headers, while this checkout
used it for a shared batch profile. Inspect the actual columns rather than
assuming that the shared revision identifier describes one schema. For the
profile-only schema, capture its current value in all existing resume versions;
older profile values were never stored and cannot be reconstructed.
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "profile" not in {column["name"] for column in inspector.get_columns("batches")}:
        op.add_column("batches", sa.Column("profile", sa.JSON()))

    added_headers = []
    for name in ("resumes", "resume_revisions"):
        if "header" not in {column["name"] for column in inspector.get_columns(name)}:
            op.add_column(name, sa.Column("header", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
            added_headers.append(name)

    if not added_headers:
        return

    metadata = sa.MetaData()
    batches = sa.Table("batches", metadata, autoload_with=bind)
    resumes = sa.Table("resumes", metadata, autoload_with=bind)
    profiles = {
        row["id"]: row["profile"] if isinstance(row["profile"], dict) else {}
        for row in bind.execute(sa.select(batches.c.id, batches.c.profile)).mappings()
    }
    headers = {
        row["id"]: profiles.get(row["batch_id"], {})
        for row in bind.execute(sa.select(resumes.c.id, resumes.c.batch_id)).mappings()
    }
    # Only newly added columns are populated. Existing per-version headers,
    # content/semantic hashes, guide snapshots and all other values stay intact.
    if "resumes" in added_headers:
        for resume_id, header in headers.items():
            bind.execute(resumes.update().where(resumes.c.id == resume_id).values(header=header))
    if "resume_revisions" in added_headers:
        history = sa.Table("resume_revisions", metadata, autoload_with=bind)
        for resume_id, header in headers.items():
            bind.execute(history.update().where(history.c.resume_id == resume_id).values(header=header))


def downgrade() -> None:
    # Both previous schemas share the same Alembic id; removing either set of
    # columns could destroy values originally created by that installation.
    raise RuntimeError("0005 cannot safely downgrade to the ambiguous 0004 schema; restore a pre-upgrade backup instead")
