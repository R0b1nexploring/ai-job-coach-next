"""Opt-in PostgreSQL checks; use only an isolated caller-supplied test database."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.db import make_engine
from app.models import Batch, FactSnapshot, MaterialRevision, Resume, TaskExecution
from app.privacy import replay_deletions
from app.privacy_v2 import V2Deleted, delete_v2_batch, v2_write_transaction
from app.service_v2 import create_v2_batch
from app.worker_v2 import _claim_batch, process_batch_v2_once
from app.generation_v2 import LocalWriterV2
from app.content_v2 import text_hash


@pytest.fixture
def pg_sessions(monkeypatch):
    url = os.getenv("COACH_PG_TEST_URL")
    if not url:
        pytest.skip("COACH_PG_TEST_URL not supplied for isolated PostgreSQL checks")
    admin = create_engine(url, hide_parameters=True)
    schema = "v2_test_" + uuid4().hex
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_url = make_url(url).update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
    monkeypatch.setenv("COACH_DATABASE_URL", scoped_url)
    monkeypatch.setenv("COACH_PROVIDER", "local")
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    try:
        command.upgrade(config, "head")
        engine = make_engine(scoped_url)
        yield sessionmaker(bind=engine, expire_on_commit=False)
        engine.dispose()
    finally:
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def make_batch(sessions):
    material = "虚构课程项目：整理设备记录。"
    value = "整理设备记录"
    start = material.index(value)
    return create_v2_batch(sessions, jd="虚构岗位负责设备记录整理、资料核对及相关协作工作。",
        material=material, candidates=[{"start": start, "end": start+len(value), "quote_hash": text_hash(value),
        "fact_type": "action", "value": value}], header={}, resume_locale="zh-CN", guide_locale="zh-CN",
        idempotency_key="pg-v2-fictional-test")["id"]


def test_pg_concurrent_claim_is_exclusive(pg_sessions):
    batch_id = make_batch(pg_sessions)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: _claim_batch(pg_sessions), range(2)))
    assert sum(claim is not None for claim in claims) == 1
    with v2_write_transaction(pg_sessions, batch_id=batch_id) as session:
        batch = session.get(Batch, batch_id)
        assert batch.status == "running" and batch.attempts == 1
        assert len(session.scalars(select(TaskExecution).where(TaskExecution.batch_id == batch_id)).all()) == 1


def test_pg_delete_during_provider_call_prevents_late_publication(pg_sessions):
    batch_id = make_batch(pg_sessions)
    class DeleteDuringGeneration(LocalWriterV2):
        def bundle(self, request):
            result = super().bundle(request)
            delete_v2_batch(pg_sessions, batch_id=batch_id)
            return result
    assert process_batch_v2_once(pg_sessions, lambda _: DeleteDuringGeneration())
    with pytest.raises(V2Deleted):
        with v2_write_transaction(pg_sessions, batch_id=batch_id):
            pass
    with pg_sessions() as session:
        assert session.get(Batch, batch_id) is None
        assert not session.scalars(select(Resume).where(Resume.batch_id == batch_id)).all()
        assert not session.scalars(select(FactSnapshot)).all()
        assert not session.scalars(select(MaterialRevision)).all()


def test_pg_durable_intent_replays_after_failed_commit_and_old_data_restore(pg_sessions):
    batch_id = make_batch(pg_sessions)
    # Preserve a synthetic pre-deletion database snapshot, then simulate a
    # failed commit after the independent intent was made durable.
    with pg_sessions() as session:
        batch = session.get(Batch, batch_id)
        tables = (FactSnapshot.__table__, MaterialRevision.__table__, Batch.__table__)
        saved = {table.name: [dict(row) for row in session.execute(select(table)).mappings()] for table in tables}
        snapshot_id = batch.fact_snapshot_id
    class FailCommit(Session):
        def commit(self):
            raise RuntimeError("fictional database failure")
    failing = sessionmaker(bind=pg_sessions.kw["bind"], class_=FailCommit)
    with pytest.raises(RuntimeError, match="fictional"):
        delete_v2_batch(failing, batch_id=batch_id)
    with pytest.raises(V2Deleted):
        with v2_write_transaction(pg_sessions, batch_id=batch_id):
            pass
    assert replay_deletions(pg_sessions) == 1
    with pg_sessions() as session:
        assert session.get(Batch, batch_id) is None
        assert session.get(FactSnapshot, snapshot_id) is None
        assert not session.scalars(select(MaterialRevision)).all()
    # Restore the old business rows with the current independent journal in
    # place; recovery must replay the original frozen deletion graph.
    with pg_sessions.begin() as session:
        for table in tables:
            if saved[table.name]:
                session.execute(table.insert(), deepcopy(saved[table.name]))
    with pytest.raises(V2Deleted):
        with v2_write_transaction(pg_sessions, batch_id=batch_id):
            pass
    assert replay_deletions(pg_sessions) == 1
    assert replay_deletions(pg_sessions) == 0
    with pg_sessions() as session:
        assert session.get(Batch, batch_id) is None
        assert session.get(FactSnapshot, snapshot_id) is None
        assert not session.scalars(select(MaterialRevision)).all()


def test_pg_guide_worker_uses_frozen_fact_and_plan_snapshots(pg_sessions):
    from app.models import Guide
    from app.worker_v2 import process_guide_v2_once

    batch_id = make_batch(pg_sessions)
    assert process_batch_v2_once(pg_sessions)
    seen = []
    class Capture(LocalWriterV2):
        def guide(self, request):
            seen.append(request)
            return super().guide(request)
    assert process_guide_v2_once(pg_sessions, lambda _: Capture())
    assert len(seen) == 1 and seen[0].kind == "blueprint"
    request = seen[0]
    assert request.facts and request.plans
    fact_ids = {item.claim_id for item in request.facts}
    plan_ids = {item.key for item in request.plans}
    assert all(set(unit.claim_refs) <= fact_ids for unit in request.units)
    assert all(unit.plan_ref is None or unit.plan_ref in plan_ids for unit in request.units)
    assert all(fact.confirmation == "unconfirmed" for fact in request.facts)
    assert all(fact.has_evidence and "manual" in fact.source_types for fact in request.facts)
    with v2_write_transaction(pg_sessions, batch_id=batch_id) as session:
        guide = session.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == "blueprint"))
        assert guide.status == "ready" and guide.sections["resume_revision"] == guide.resume_revision
        assert all(unit_id in guide.sections["coverage_unit_ids"] for entry in guide.sections["entries"] for unit_id in entry["unit_ids"])


def test_pg_upgrade_nonempty_0005_keeps_exact_v1_values(monkeypatch):
    """Exercise the immediate predecessor with persisted v1 revision data."""
    from datetime import datetime, timezone
    from sqlalchemy import MetaData, Table

    url = os.getenv("COACH_PG_TEST_URL")
    if not url:
        pytest.skip("COACH_PG_TEST_URL not supplied for isolated PostgreSQL checks")
    admin = create_engine(url, hide_parameters=True)
    schema = "v2_upgrade_" + uuid4().hex
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    scoped_url = make_url(url).update_query_dict({"options": f"-csearch_path={schema}"}).render_as_string(hide_password=False)
    monkeypatch.setenv("COACH_DATABASE_URL", scoped_url)
    config = Config("alembic.ini")
    config.set_main_option("script_location", "alembic")
    engine = make_engine(scoped_url)
    try:
        command.upgrade(config, "0005")
        metadata = MetaData()
        batches = Table("batches", metadata, autoload_with=engine)
        resumes = Table("resumes", metadata, autoload_with=engine)
        revisions = Table("resume_revisions", metadata, autoload_with=engine)
        now = datetime.now(timezone.utc)
        resume = {"id": "r" * 32, "batch_id": "b" * 32, "kind": "job", "title": "虚构旧稿", "summary": "虚构摘要",
                  "header": {"name": "虚构人物"}, "items": [{"id": "old-item", "text": "虚构项目"}],
                  "content_hash": "a" * 64, "semantic_hash": "b" * 64, "plans": [], "revision": 1, "created_at": now}
        with engine.begin() as session:
            session.execute(batches.insert().values(id="b" * 32, idempotency_key="pg-0005-fixture", input_hash="c" * 64,
                jd_text="虚构岗位", material_text="虚构材料", material_confirmed=False, status="ready", attempts=1,
                created_at=now, updated_at=now, profile={"name": "虚构人物"}))
            session.execute(resumes.insert().values(**resume))
            session.execute(revisions.insert().values(id="h" * 32, resume_id=resume["id"], revision=1,
                title=resume["title"], summary=resume["summary"], header=resume["header"], items=resume["items"],
                content_hash=resume["content_hash"], semantic_hash=resume["semantic_hash"], plans=[], created_at=now))
            before = {table.name: session.execute(select(table)).all() for table in (batches, resumes, revisions)}
        command.upgrade(config, "head")
        with engine.connect() as session:
            for table in (batches, resumes, revisions):
                assert session.execute(select(table)).all() == before[table.name]
            current = Table("resumes", MetaData(), autoload_with=session)
            row = session.execute(select(current)).mappings().one()
            assert row["schema_version"] is None and row["document_v2"] is None
        command.check(config)
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_pg_preserve_existing_material_retirement_keeps_snapshot_and_blocks_reuse(pg_sessions):
    from app.materials_v2 import create_independent_material, list_independent_materials, read_material_revision
    from app.privacy_v2 import delete_independent_material, preview_independent_material_deletion
    from app.service_v2 import get_v2_batch

    source_text = "虚构学习项目：整理设备记录。"
    value = "整理设备记录"
    start = source_text.index(value)
    source = create_independent_material(pg_sessions, extracted_text=source_text, source_type="manual")
    candidates = [{"start": start, "end": start + len(value), "quote_hash": text_hash(value),
                   "fact_type": "action", "value": value, "confirmed": True}]
    args = {"jd": "虚构岗位负责设备记录整理、资料核对及相关协作工作。", "material": source_text,
            "candidates": candidates, "header": {}, "resume_locale": "zh-CN", "guide_locale": "zh-CN",
            "source_type": "manual", "material_revision_id": source["revision_id"]}
    batch_id = create_v2_batch(pg_sessions, idempotency_key="pg-reuse-before-retire", **args)["id"]
    preview = preview_independent_material_deletion(pg_sessions, material_id=source["material_id"], mode="preserve_existing")
    assert preview["affected_batch_ids"] == [batch_id]
    delete_independent_material(pg_sessions, material_id=source["material_id"], mode="preserve_existing",
                                expected_impact_token=preview["impact_token"])
    # The existing batch's evidence is a frozen copy; deletion prevents source
    # reuse but does not rewrite or remove that explicitly retained batch.
    view = get_v2_batch(pg_sessions, batch_id)
    assert view["facts"][0]["value"] == value and view["facts"][0]["confirmation"] == "user_confirmed"
    with v2_write_transaction(pg_sessions, batch_id=batch_id) as session:
        batch = session.get(Batch, batch_id)
        snapshot = session.get(FactSnapshot, batch.fact_snapshot_id)
        assert snapshot.source_copies[0]["extracted_text"] == source_text
        assert session.get(MaterialRevision, source["revision_id"]) is None
    assert list_independent_materials(pg_sessions) == []
    with pytest.raises(V2Deleted):
        read_material_revision(pg_sessions, material_id=source["material_id"], revision_id=source["revision_id"])
    with pytest.raises(V2Deleted):
        create_v2_batch(pg_sessions, idempotency_key="pg-reuse-after-retire", **args)
    # Durable retirement and existing-batch preservation also survive replay.
    assert replay_deletions(pg_sessions) == 0
    assert get_v2_batch(pg_sessions, batch_id)["facts"][0]["value"] == value
