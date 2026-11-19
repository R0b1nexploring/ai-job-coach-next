from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import MetaData, Table, create_engine, inspect, select
from sqlalchemy.orm import Session

from app.models import FactSnapshot, MaterialRevision

ROOT = Path(__file__).resolve().parents[1]


def test_v1_upgrade_preserves_data_and_v2_snapshots_are_distinct(tmp_path, monkeypatch):
    url = f"sqlite:///{tmp_path / 'fictional.sqlite'}"
    monkeypatch.setenv("COACH_DATABASE_URL", url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    engine = create_engine(url)
    command.upgrade(config, "0005")
    old_batch = Table("batches", MetaData(), autoload_with=engine)
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(old_batch.insert().values(
            id="legacy", idempotency_key="legacy-key", input_hash="a" * 64,
            jd_text="虚构岗位", material_text="虚构材料", material_confirmed=False,
            status="ready", attempts=1, created_at=now, updated_at=now,
        ))
        before = conn.execute(select(old_batch)).one()
    command.upgrade(config, "head")
    with engine.connect() as conn:
        assert conn.execute(select(old_batch)).one() == before
        batch = Table("batches", MetaData(), autoload_with=conn)
        row = conn.execute(select(batch)).mappings().one()
        assert row["schema_version"] is None
        assert row["fact_snapshot_id"] is None
        assert row["target_snapshot"] is None
    # Each edit/confirmation can be represented by a new immutable record;
    # no old record is modified during migration or snapshot creation.
    with Session(engine) as session:
        session.add_all([
            MaterialRevision(id="m1", material_id="m", revision=1, scope_id="local", source_type="text", extracted_text="虚构项目", content_hash="b" * 64),
            MaterialRevision(id="m2", material_id="m", revision=2, scope_id="local", source_type="text", extracted_text="虚构项目修订", content_hash="c" * 64),
            FactSnapshot(id="f1", scope_id="local", source_copies=[{"revision_id": "m1", "extracted_text": "虚构项目", "content_hash": "b" * 64}], claims=[{"claim_id": "c1", "confirmation": "unconfirmed", "evidence": [{"source_revision_id": "m1", "start": 0, "end": 4}]}]),
            FactSnapshot(id="f2", scope_id="local", source_copies=[], claims=[{"claim_id": "c1", "confirmation": "user_confirmed", "evidence": [{"source_revision_id": "m1", "start": 0, "end": 4}]}]),
        ])
        session.commit()
    with Session(engine) as session:
        assert session.get(MaterialRevision, "m1").extracted_text == "虚构项目"
        assert session.get(FactSnapshot, "f1").claims[0]["confirmation"] == "unconfirmed"
        assert session.get(FactSnapshot, "f1").source_copies[0]["extracted_text"] == "虚构项目"
        assert session.get(FactSnapshot, "f2").source_copies == []
        assert session.get(FactSnapshot, "f2").claims[0]["confirmation"] == "user_confirmed"
    inspector = inspect(engine)
    for table, column in (("batches", "generation_input_hash"), ("guides", "guide_input_hash"), ("guide_revisions", "guide_input_hash")):
        columns = {col["name"]: col for col in inspector.get_columns(table)}
        assert columns[column]["type"].length >= len("guide-input-v2:" + "a" * 64)
    command.check(config)
    with pytest.raises(RuntimeError, match="cannot safely downgrade"):
        command.downgrade(config, "0005")
    engine.dispose()
