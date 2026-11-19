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


@pytest.fixture(params=["sqlite", "postgresql"])
def migration_db(request, tmp_path, monkeypatch):
    # SQLite always runs. PostgreSQL is additional coverage only when a caller
    # explicitly supplies a test database; never discover daily config/data.
    pg_url = os.getenv("COACH_PG_TEST_URL") if request.param == "postgresql" else None
    if request.param == "postgresql" and not pg_url:
        pytest.skip("COACH_PG_TEST_URL not provided for isolated PostgreSQL migration checks")
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


def _insert_0003_fixture(config, engine):
    """Store multiple fictional revisions, guides and frozen task inputs."""
    command.upgrade(config, "0003")
    metadata = MetaData()
    tables = {
        name: Table(name, metadata, autoload_with=engine)
        for name in ("batches", "resumes", "resume_revisions", "guides", "guide_revisions", "task_executions")
    }
    now = utcnow()
    with engine.begin() as connection:
        connection.execute(tables["batches"].insert().values(
            id="fictional-batch", idempotency_key="fictional-0004-compatibility",
            input_hash="0" * 64, jd_text=JD, material_text=MATERIAL,
            material_confirmed=False, status="ready", attempts=1,
            plans={"projects": []}, created_at=now, updated_at=now,
        ))
        for kind in ("job", "blueprint"):
            resume_id = "fictional-" + kind
            snapshots = []
            for revision in (1, 2):
                data = {"title": "虚构历史简历", "summary": f"虚构修订 {revision}", "items": [{"id": "fictional-item", "text": f"虚构项目 {revision}", "verification_status": "ai_generated", "achieved": False}]}
                content_hash = stable_hash(data)
                semantic_hash = stable_hash({"data": data, "kind": kind, "revision": revision})
                snapshots.append({**data, "content_hash": content_hash, "semantic_hash": semantic_hash, "plans": [], "created_at": now})
            current = snapshots[-1]
            connection.execute(tables["resumes"].insert().values(
                id=resume_id, batch_id="fictional-batch", kind=kind,
                revision=2, **current,
            ))
            for revision, snapshot in enumerate(snapshots, start=1):
                connection.execute(tables["resume_revisions"].insert().values(
                    id=f"history-{kind}-{revision}", resume_id=resume_id,
                    revision=revision, **snapshot,
                ))
            guide_id = "guide-" + kind
            connection.execute(tables["guides"].insert().values(
                id=guide_id, batch_id="fictional-batch", resume_id=resume_id,
                kind=kind, status="ready", content_hash=current["content_hash"],
                semantic_hash=current["semantic_hash"], revision=1,
                resume_revision=2, sections={"entries": [{"item_id": "fictional-item", "answer": "虚构指南"}]}, attempts=1,
            ))
            frozen = {key: current[key] for key in ("title", "summary", "items")}
            connection.execute(tables["guide_revisions"].insert().values(
                id="guide-history-" + kind, guide_id=guide_id,
                batch_id="fictional-batch", revision=1, resume_revision=2,
                content_hash=current["content_hash"], semantic_hash=current["semantic_hash"],
                resume_snapshot=frozen, plans=[], jd_text=JD, status="ready",
                sections={"entries": [{"item_id": "fictional-item", "answer": "虚构指南"}]},
                created_at=now, completed_at=now,
            ))
            connection.execute(tables["task_executions"].insert().values(
                id="execution-" + kind, batch_id="fictional-batch", guide_id=guide_id,
                task_type="guide", claim_token="fictional-token-" + kind,
                attempt=1, input_hash=current["semantic_hash"],
                input_snapshot={"resume": frozen, "plans": [], "jd": JD},
                status="ready", started_at=now, finished_at=now,
            ))
    return tables


def _snapshot_original_columns(engine):
    snapshot = {}
    metadata = MetaData()
    with engine.connect() as connection:
        for name in inspect(engine).get_table_names():
            if name == "alembic_version":
                continue
            table = Table(name, metadata, autoload_with=engine)
            snapshot[name] = (table, connection.execute(select(table).order_by(table.c.id)).all())
    return snapshot


def _assert_original_columns_preserved(engine, snapshot):
    with engine.connect() as connection:
        for name, (table, rows) in snapshot.items():
            assert connection.execute(select(table).order_by(table.c.id)).all() == rows, name


@pytest.mark.parametrize("prior_schema", ["0003", "0004_profile", "0004_header", "0004_both"])
def test_compatibility_upgrade_preserves_all_original_values(migration_db, prior_schema):
    config, engine, schema = migration_db
    _insert_0003_fixture(config, engine)
    profile = {"name": "虚构资料", "phone": "13800000000", "email": "fixture@example.com", "education": "虚构大学 工程专业"}
    if prior_schema in {"0004_header", "0004_both"}:
        command.upgrade(config, "0004")
    if prior_schema in {"0004_profile", "0004_both"}:
        # The independent profile-only branch used the same Alembic revision ID.
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE batches ADD COLUMN profile JSON"))
            batches = Table("batches", MetaData(), autoload_with=connection)
            connection.execute(batches.update().values(profile=profile))
    if prior_schema == "0004_profile":
        command.stamp(config, "0004")
    if prior_schema in {"0004_header", "0004_both"}:
        with engine.begin() as connection:
            for name in ("resumes", "resume_revisions"):
                table = Table(name, MetaData(), autoload_with=connection)
                for row in connection.execute(select(table.c.id)).mappings():
                    # Differing current/historical values prove the migration
                    # never replaces existing headers with one shared profile.
                    connection.execute(table.update().where(table.c.id == row["id"]).values(header={"name": "虚构 " + row["id"], "city": "虚构城市"}))
    before = _snapshot_original_columns(engine)
    command.upgrade(config, "head")
    _assert_original_columns_preserved(engine, before)
    for name in ("resumes", "resume_revisions"):
        columns = {column["name"]: column for column in inspect(engine).get_columns(name)}
        assert columns["header"]["nullable"] is False
        assert columns["header"]["default"] is not None
        table = Table(name, MetaData(), autoload_with=engine)
        with engine.connect() as connection:
            rows = connection.execute(select(table.c.id, table.c.header)).mappings().all()
            assert rows
            if prior_schema == "0004_profile":
                assert all(row["header"] == profile for row in rows)
            elif prior_schema == "0003":
                assert all(row["header"] == {} for row in rows)
            else:
                assert all(row["header"] == {"name": "虚构 " + row["id"], "city": "虚构城市"} for row in rows)
    batches = Table("batches", MetaData(), autoload_with=engine)
    with engine.connect() as connection:
        assert connection.scalar(select(batches.c.profile)) == (profile if prior_schema in {"0004_profile", "0004_both"} else None)
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0007"
    command.check(config)
    _assert_no_compatibility_objects(engine)


def test_compatibility_downgrade_refuses_ambiguous_column_removal(migration_db):
    config, engine, schema = migration_db
    command.upgrade(config, "0005")
    with pytest.raises(RuntimeError, match="ambiguous 0004 schema"):
        command.downgrade(config, "0004")
    assert "header" in {column["name"] for column in inspect(engine).get_columns("resumes")}
    assert "profile" in {column["name"] for column in inspect(engine).get_columns("batches")}
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0005"


def test_0006_material_ownership_remains_unknown(migration_db):
    config, engine, _ = migration_db
    command.upgrade(config, "0006")
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO material_revisions (id, material_id, revision, scope_id, source_type, extracted_text, content_hash, created_at) VALUES (:id, :material_id, 1, 'local', 'text', 'fiction', :digest, :created_at)"),
                           {"id": "a" * 32, "material_id": "b" * 32, "digest": "c" * 64, "created_at": "2026-01-01 00:00:00"})
    command.upgrade(config, "head")
    with engine.connect() as connection:
        row = connection.execute(text("SELECT retention, owner_batch_id, extracted_text FROM material_revisions WHERE id = :id"), {"id": "a" * 32}).one()
        assert tuple(row) == (None, None, "fiction")
