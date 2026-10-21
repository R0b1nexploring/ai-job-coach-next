from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
import pytest
from sqlalchemy import MetaData, Table, create_engine, inspect, select, text
from sqlalchemy.engine import make_url

from app.content import normalize_bundle, normalize_guide, stable_hash
from app.models import utcnow
from app.providers import LocalProvider

ROOT = Path(__file__).resolve().parents[1]
JD = "虚构机械岗位：负责装配结构设计与方案验证。"
MATERIAL = "虚构课程项目：绘制装配图并整理零件清单。"


@pytest.fixture
def migration_db(tmp_path, monkeypatch):
    pg_url = os.getenv("COACH_PG_TEST_URL")
    admin = None
    schema = None
    if pg_url:
        schema = "migration_" + uuid4().hex
        admin = create_engine(pg_url)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        url = make_url(pg_url).update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
    else:
        url = f"sqlite:///{tmp_path / 'migration.sqlite'}"
    monkeypatch.setenv("COACH_DATABASE_URL", url)
    engine = create_engine(url)
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    try:
        yield config, engine, schema
    finally:
        engine.dispose()
        if admin:
            with admin.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()


def _assert_no_compatibility_objects(engine):
    if engine.dialect.name == "postgresql":
        with engine.connect() as connection:
            assert not connection.scalar(text("SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname LIKE 'coach_migration_%')"))


def test_fresh_migration_and_schema_matches_models(migration_db):
    config, engine, schema = migration_db
    command.upgrade(config, "head")
    assert set(inspect(engine).get_table_names()) >= {"batches", "resumes", "resume_revisions", "guides", "guide_revisions", "task_executions"}
    command.check(config)
    _assert_no_compatibility_objects(engine)


def test_upgrade_0001_data_preserves_resume_and_guide_relationships(migration_db):
    config, engine, schema = migration_db
    command.upgrade(config, "0001")
    metadata = MetaData()
    batches = Table("batches", metadata, autoload_with=engine)
    resumes = Table("resumes", metadata, autoload_with=engine)
    guides = Table("guides", metadata, autoload_with=engine)
    provider = LocalProvider()
    bundle = normalize_bundle(provider.bundle(JD, MATERIAL), JD, MATERIAL, False)
    batch_id = uuid4().hex
    now = utcnow()
    resume_ids = {}
    with engine.begin() as connection:
        connection.execute(batches.insert().values(id=batch_id, idempotency_key="migration-fiction", input_hash=stable_hash({"jd": JD, "material": MATERIAL, "confirmed": False}), jd_text=JD, material_text=MATERIAL, material_confirmed=False, status="ready", attempts=1, created_at=now, updated_at=now, plans={"projects": bundle["plans"], "source": "local"}))
        for kind, data in bundle["resumes"].items():
            resume_id = uuid4().hex
            resume_ids[kind] = resume_id
            content_hash = stable_hash(data)
            entries = normalize_guide(provider.guide(kind, data, bundle["plans"], JD), data, kind)
            connection.execute(resumes.insert().values(id=resume_id, batch_id=batch_id, kind=kind, title=data["title"], summary=data["summary"], items=data["items"], content_hash=content_hash, created_at=now))
            connection.execute(guides.insert().values(id=uuid4().hex, batch_id=batch_id, resume_id=resume_id, kind=kind, status="ready", content_hash=content_hash, sections={"entries": entries, "source": "local"}, attempts=1))
    command.upgrade(config, "head")
    current = MetaData()
    upgraded = Table("resumes", current, autoload_with=engine)
    revisions = Table("resume_revisions", current, autoload_with=engine)
    guide_history = Table("guide_revisions", current, autoload_with=engine)
    with engine.connect() as connection:
        rows = connection.execute(select(upgraded)).mappings().all()
        assert {row["id"] for row in rows} == set(resume_ids.values())
        assert all(row["plans"] and len(row["semantic_hash"]) == 64 for row in rows)
        previous = connection.execute(select(revisions)).mappings().all()
        assert len(previous) == 2
        assert all(row["revision"] == 1 and row["plans"] for row in previous)
        history = connection.execute(select(guide_history)).mappings().all()
        assert len(history) == 2
        assert all(row["resume_revision"] == 1 and row["status"] == "ready" for row in history)
        assert {row["sections"]["entries"][0]["item_id"] for row in history} == {data["items"][0]["id"] for data in bundle["resumes"].values()}
    command.check(config)
    _assert_no_compatibility_objects(engine)


def test_upgrade_0003_adds_empty_headers_without_rewriting_existing_data(migration_db):
    config, engine, schema = migration_db
    command.upgrade(config, "0003")
    metadata = MetaData()
    tables = {name: Table(name, metadata, autoload_with=engine) for name in (
        "batches", "resumes", "resume_revisions", "guides", "guide_revisions",
    )}
    batch_id, resume_id, guide_id = (uuid4().hex for _ in range(3))
    now = utcnow()
    data = normalize_bundle(LocalProvider().bundle(JD, MATERIAL), JD, MATERIAL, False)["resumes"]["job"]
    fingerprint = "7" * 64
    content_hash = "8" * 64
    with engine.begin() as connection:
        connection.execute(tables["batches"].insert().values(
            id=batch_id, idempotency_key="fictional-header-migration", input_hash="6" * 64,
            jd_text=JD, material_text=MATERIAL, material_confirmed=False, status="ready", attempts=1,
            created_at=now, updated_at=now, plans={"projects": [], "source": "local"},
        ))
        values = dict(title=data["title"], summary=data["summary"], items=data["items"], plans=[],
                      content_hash=content_hash, semantic_hash=fingerprint, revision=1, created_at=now)
        connection.execute(tables["resumes"].insert().values(id=resume_id, batch_id=batch_id, kind="job", **values))
        connection.execute(tables["resume_revisions"].insert().values(id=uuid4().hex, resume_id=resume_id, **values))
        connection.execute(tables["guides"].insert().values(
            id=guide_id, batch_id=batch_id, resume_id=resume_id, kind="job", status="ready",
            content_hash=content_hash, semantic_hash=fingerprint, revision=1, resume_revision=1,
            attempts=1, sections={"entries": [], "source": "local"},
        ))
        connection.execute(tables["guide_revisions"].insert().values(
            id=uuid4().hex, guide_id=guide_id, batch_id=batch_id, revision=1, resume_revision=1,
            content_hash=content_hash, semantic_hash=fingerprint, resume_snapshot=data, plans=[], jd_text=JD,
            status="ready", sections={"entries": [], "source": "local"}, created_at=now, completed_at=now,
        ))
        previous = {name: connection.execute(select(table)).mappings().all() for name, table in tables.items()}
    command.upgrade(config, "head")
    upgraded_metadata = MetaData()
    with engine.connect() as connection:
        for name, old_table in tables.items():
            upgraded = Table(name, upgraded_metadata, autoload_with=engine)
            observed = connection.execute(select(*(upgraded.c[column.name] for column in old_table.columns))).mappings().all()
            assert observed == previous[name]
            if name in {"resumes", "resume_revisions"}:
                assert connection.execute(select(upgraded.c.header)).scalars().all() == [{}]
        history = Table("guide_revisions", upgraded_metadata, autoload_with=engine)
        assert "header" not in connection.execute(select(history.c.resume_snapshot)).scalar_one()
    command.check(config)
    _assert_no_compatibility_objects(engine)
