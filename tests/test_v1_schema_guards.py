from copy import deepcopy

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app import api
from app.db import Base
from app.models import Batch, Guide, GuideRevision, Resume, ResumeRevision, TaskExecution, utcnow
from app.providers import LocalProvider
from app.service import create_batch, process_batch_once, process_guide_once

JD = "虚构机械岗位负责设备装配设计、工程图输出和质量验证工作。"
MATERIAL = "虚构课程项目：绘制装配图并整理零件清单。"


def setup(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'guard.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(api, "SessionLocal", sessions)
    return engine, sessions, TestClient(api.app)


def test_v1_routes_refuse_v2_and_preserve_old_historical_revision(tmp_path, monkeypatch):
    engine, sessions, client = setup(tmp_path, monkeypatch)
    provider = LocalProvider()
    batch = create_batch(sessions, jd=JD, material=MATERIAL, confirmed=False, idempotency_key="v1-guard-test")
    assert process_batch_once(sessions, provider)
    assert process_guide_once(sessions, provider)
    assert process_guide_once(sessions, provider)
    original = client.get(f"/api/batches/{batch.id}").json()["resumes"]["job"]
    with sessions() as session:
        resume = session.scalar(select(Resume).where(Resume.batch_id == batch.id, Resume.kind == "job"))
        original_items = deepcopy(resume.items)
        resume.schema_version = 2
        resume.document_v2 = {"schema_version": 2, "sections": [{"type": "experience"}]}
        resume.revision = 2
        session.add(ResumeRevision(resume_id=resume.id, revision=2, title=resume.title, summary=resume.summary,
            header=resume.header, items=[], plans=resume.plans, content_hash=resume.content_hash,
            semantic_hash=resume.semantic_hash, schema_version=2, document_v2=resume.document_v2))
        session.commit()
    paths = [f"/api/batches/{batch.id}", "/api/batches/latest", "/api/batches",
             f"/api/batches/{batch.id}/archive", f"/api/batches/{batch.id}/resumes/job/revisions",
             f"/api/batches/{batch.id}/resumes/job/download/html",
             f"/api/batches/{batch.id}/resumes/job/download/html?revision=2"]
    for path in paths:
        response = client.get(path)
        assert response.status_code == 409, path
        assert response.json()["code"] == "CLIENT_UPGRADE_REQUIRED"
    old = client.get(f"/api/batches/{batch.id}/resumes/job/download/html?revision=1")
    assert old.status_code == 200
    response = client.put(f"/api/batches/{batch.id}/resumes/job", json={
        "revision": 2, "title": original["title"], "summary": original["summary"], "items": original["items"]})
    assert response.status_code == 409 and response.json()["code"] == "CLIENT_UPGRADE_REQUIRED"
    assert client.post(f"/api/batches/{batch.id}/guides/job/retry").json()["code"] == "CLIENT_UPGRADE_REQUIRED"
    assert client.delete(f"/api/batches/{batch.id}").json()["code"] == "CLIENT_UPGRADE_REQUIRED"
    with sessions() as session:
        resume = session.scalar(select(Resume).where(Resume.batch_id == batch.id, Resume.kind == "job"))
        assert resume.revision == 2 and resume.items == original_items
        assert resume.document_v2["sections"][0]["type"] == "experience"
    engine.dispose()


def test_v1_worker_skips_v2_and_unknown_schema_without_starving_v1(tmp_path, monkeypatch):
    engine, sessions, _ = setup(tmp_path, monkeypatch)
    v2 = create_batch(sessions, jd=JD, material=MATERIAL, confirmed=False, idempotency_key="queued-v2")
    future = create_batch(sessions, jd=JD, material=MATERIAL, confirmed=False, idempotency_key="queued-v3")
    v1 = create_batch(sessions, jd=JD, material=MATERIAL, confirmed=False, idempotency_key="queued-v1")
    with sessions() as session:
        session.get(Batch, v2.id).schema_version = 2
        session.get(Batch, future.id).schema_version = 3
        session.commit()
    assert process_batch_once(sessions, LocalProvider())
    with sessions() as session:
        assert session.get(Batch, v1.id).status == "guides_pending"
        assert session.get(Batch, v2.id).status == session.get(Batch, future.id).status == "queued"
        assert session.get(Batch, v2.id).attempts == session.get(Batch, future.id).attempts == 0
        guide = session.scalar(select(Guide).where(Guide.batch_id == v1.id, Guide.kind == "job"))
        rev = session.scalar(select(GuideRevision).where(GuideRevision.guide_id == guide.id))
        rev.schema_version = 2
        other = session.scalar(select(Guide).where(Guide.batch_id == v1.id, Guide.kind == "blueprint"))
        other.status = "ready"
        session.commit()
    assert not process_guide_once(sessions, LocalProvider())
    with sessions() as session:
        guide = session.scalar(select(Guide).where(Guide.batch_id == v1.id, Guide.kind == "job"))
        assert guide.status == "queued" and guide.attempts == 0
        assert not session.scalar(select(TaskExecution.id).where(TaskExecution.guide_id == guide.id))
    engine.dispose()


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("task", ["batch", "guide"])
def test_late_v1_worker_cannot_publish_after_v2_switch(tmp_path, monkeypatch, failure, task):
    engine, sessions, _ = setup(tmp_path, monkeypatch)
    batch = create_batch(sessions, jd=JD, material=MATERIAL, confirmed=False, idempotency_key="late-v2-switch")
    if task == "guide":
        assert process_batch_once(sessions, LocalProvider())
    class SwitchingProvider(LocalProvider):
        def bundle(self, jd, material):
            output = super().bundle(jd, material)
            with sessions() as session:
                row = session.get(Batch, batch.id)
                row.schema_version = 2
                row.target_snapshot = {"marker": "v2-data"}
                session.commit()
            if failure:
                raise RuntimeError("fictional failure")
            return output

        def guide(self, kind, resume, plans, jd):
            output = super().guide(kind, resume, plans, jd)
            with sessions() as session:
                guide = session.scalar(select(Guide).where(Guide.batch_id == batch.id, Guide.kind == kind))
                revision = session.scalar(select(GuideRevision).where(GuideRevision.guide_id == guide.id, GuideRevision.revision == guide.revision))
                revision.schema_version = 2
                revision.v2_snapshot = {"marker": "v2-data"}
                session.commit()
            if failure:
                raise RuntimeError("fictional failure")
            return output

    if task == "batch":
        assert process_batch_once(sessions, SwitchingProvider())
        with sessions() as session:
            row = session.get(Batch, batch.id)
            assert row.schema_version == 2 and row.target_snapshot == {"marker": "v2-data"}
            assert row.status == "running" and row.error_code is None
            assert not session.scalar(select(Resume.id).where(Resume.batch_id == batch.id))
    else:
        assert process_guide_once(sessions, SwitchingProvider())
        with sessions() as session:
            guide = session.scalar(select(Guide).where(Guide.batch_id == batch.id, Guide.kind == "job"))
            revision = session.scalar(select(GuideRevision).where(GuideRevision.guide_id == guide.id))
            assert guide.status == revision.status == "running"
            assert guide.sections is None and revision.sections is None
            assert revision.v2_snapshot == {"marker": "v2-data"}
    engine.dispose()
