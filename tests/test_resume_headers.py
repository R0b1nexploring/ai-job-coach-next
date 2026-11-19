"""Fictional header revision, privacy, export, and concurrent-save contracts."""
from copy import deepcopy
from io import BytesIO
import json
import os
from uuid import uuid4

from docx import Document
from fastapi.testclient import TestClient
from pypdf import PdfReader
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

from app import api
from app.db import Base
from app.models import GuideRevision, Resume, TaskExecution
from app.providers import LocalProvider
from app.service import create_batch, get_batch, list_revisions, process_batch_once, process_guide_once, retry_guide, save_resume, semantic_hash

JD = "虚构机械岗位：负责装配结构设计、图纸输出和方案验证。"
MATERIAL = "虚构课程项目：绘制装配图并整理零部件清单。"
HEADER = {"name": "虚构甲", "phone": "13800000000", "email": "fictional@example.com", "location": "虚构城市", "education": "虚构学校\n机械专业 本科"}


@pytest.fixture
def workbench(tmp_path, monkeypatch):
    url = os.getenv("COACH_PG_TEST_URL")
    engine = create_engine(url or f"sqlite:///{tmp_path / 'headers.sqlite'}")
    schema = "headers_" + uuid4().hex if url else None
    if schema:
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped = engine.execution_options(schema_translate_map={None: schema})
    else:
        scoped = engine
    Base.metadata.create_all(scoped)
    sessions = sessionmaker(bind=scoped, expire_on_commit=False)
    monkeypatch.setattr(api, "SessionLocal", sessions)
    batch = create_batch(sessions, jd=JD, material=MATERIAL, confirmed=True, idempotency_key="fictional-header", profile=HEADER)
    assert process_batch_once(sessions, LocalProvider())
    assert process_guide_once(sessions, LocalProvider())
    assert process_guide_once(sessions, LocalProvider())
    try:
        yield sessions, TestClient(api.app), batch.id
    finally:
        if schema:
            with engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def body(resume, **updates):
    values = {key: deepcopy(resume[key]) for key in ("revision", "title", "summary", "items", "plans")}
    return {**values, **updates}


def test_header_edit_clear_and_noop_preserve_other_version_and_guide(workbench):
    sessions, client, identifier = workbench
    before = get_batch(sessions, identifier)
    original = before["resumes"]["job"]
    edited = client.put(f"/api/batches/{identifier}/resumes/job", json=body(original, header={"name": "虚构乙", "location": "另一虚构城市"}))
    assert edited.status_code == 200
    state = edited.json()
    assert state["resumes"]["job"]["revision"] == 2
    assert state["resumes"]["blueprint"] == before["resumes"]["blueprint"]
    assert state["resumes"]["job"]["content_hash"] != original["content_hash"]
    assert state["resumes"]["job"]["semantic_hash"] == original["semantic_hash"]
    assert state["guides"]["job"]["status"] == "ready"
    assert state["guides"]["job"]["resume_revision"] == 1
    assert state["guides"]["job"]["applies_to_revision"] == 2
    assert list_revisions(sessions, identifier, "job")[-1]["header"] == HEADER

    # Omitted/None headers retain saved values; unchanged saves create no revision.
    response = client.put(f"/api/batches/{identifier}/resumes/job", json=body(state["resumes"]["job"]))
    assert response.status_code == 200
    assert response.json()["resumes"]["job"]["revision"] == 2
    assert response.json()["resumes"]["job"]["header"] == {"name": "虚构乙", "location": "另一虚构城市"}
    cleared = client.put(f"/api/batches/{identifier}/resumes/job", json=body(response.json()["resumes"]["job"], header={}))
    assert cleared.status_code == 200
    assert cleared.json()["resumes"]["job"]["header"] == {}
    assert cleared.json()["resumes"]["job"]["revision"] == 3
    assert cleared.json()["guides"]["job"]["applies_to_revision"] == 3
    assert cleared.json()["resumes"]["blueprint"]["header"] == HEADER
    assert client.get(f"/api/batches/{identifier}").json() == cleared.json()


@pytest.mark.parametrize("format", ["html", "docx", "pdf"])
def test_current_and_historical_downloads_own_their_header(workbench, format):
    sessions, client, identifier = workbench
    current = get_batch(sessions, identifier)["resumes"]["job"]
    assert client.put(f"/api/batches/{identifier}/resumes/job", json=body(current, header={})).status_code == 200

    def visible(response):
        assert response.status_code == 200
        if format == "html":
            return response.text
        if format == "docx":
            return "\n".join(p.text for p in Document(BytesIO(response.content)).paragraphs)
        return "\n".join(page.extract_text() for page in PdfReader(BytesIO(response.content)).pages)

    route = f"/api/batches/{identifier}/resumes/job/download/{format}"
    old = visible(client.get(route + "?revision=1"))
    new = visible(client.get(route))
    assert all(value in old for value in ("虚构甲", "13800000000", "fictional@example.com", "虚构城市", "虚构学校"))
    assert all(value not in new for value in ("虚构甲", "13800000000", "fictional@example.com", "虚构城市", "虚构学校"))
    assert all(label in new for label in ("姓名", "电话", "邮箱", "城市", "教育背景"))
    blueprint = visible(client.get(f"/api/batches/{identifier}/resumes/blueprint/download/{format}"))
    assert "虚构甲" in blueprint and "尚未完成" in blueprint


def test_stale_header_save_is_rejected_and_bad_header_does_not_write(workbench):
    sessions, client, identifier = workbench
    current = get_batch(sessions, identifier)["resumes"]["job"]
    route = f"/api/batches/{identifier}/resumes/job"
    invalid = ({"name": "x" * 81}, {"email": "x" * 161}, {"location": "x" * 101}, {"education": "x" * 1501}, {"company": "虚构单位"}, {"name": "虚构甲\n虚构乙"}, {"name": 3})
    for header in invalid:
        assert client.put(route, json=body(current, header=header)).status_code == 422
    assert get_batch(sessions, identifier)["resumes"]["job"] == current
    assert client.put(route, json=body(current, header={"name": "虚构乙"})).status_code == 200
    assert client.put(route, json=body(current, header={"name": "虚构丙"})).status_code == 409
    assert get_batch(sessions, identifier)["resumes"]["job"]["header"] == {"name": "虚构乙"}


def test_headers_never_enter_model_inputs_or_guide_execution_snapshots(workbench):
    sessions, _, identifier = workbench
    current = get_batch(sessions, identifier)["resumes"]["job"]
    save_resume(sessions, identifier, "job", **body(current, header={"name": "虚构私有甲", "email": "private-fictional@example.com"}))
    retry_guide(sessions, identifier, "job")
    captured = []

    class Inspect(LocalProvider):
        def guide(self, kind, resume, plans, jd):
            captured.append(deepcopy(resume))
            assert set(resume) == {"title", "summary", "items"}
            return super().guide(kind, resume, plans, jd)

    assert process_guide_once(sessions, Inspect())
    assert captured
    with sessions() as session:
        snapshots = list(session.scalars(select(GuideRevision.resume_snapshot)))
        snapshots += list(session.scalars(select(TaskExecution.input_snapshot)))
    serialized = json.dumps(snapshots, ensure_ascii=False)
    assert "虚构私有甲" not in serialized and "private-fictional@example.com" not in serialized
    assert "fictional@example.com" not in serialized


def test_header_edit_during_guide_generation_does_not_supersede_it(workbench):
    sessions, _, identifier = workbench
    retry_guide(sessions, identifier, "job")

    class EditDuringCall(LocalProvider):
        def guide(self, kind, resume, plans, jd):
            current = get_batch(sessions, identifier)["resumes"]["job"]
            save_resume(sessions, identifier, "job", **body(current, header={"name": "虚构乙"}))
            return super().guide(kind, resume, plans, jd)

    assert process_guide_once(sessions, EditDuringCall())
    state = get_batch(sessions, identifier)
    assert state["resumes"]["job"]["revision"] == 2
    assert state["guides"]["job"]["status"] == "ready"
    assert state["guides"]["job"]["resume_revision"] == 1
    assert state["guides"]["job"]["applies_to_revision"] == 2


def test_header_only_ui_save_keeps_legacy_confirmed_items_semantically_identical(workbench):
    sessions, client, identifier = workbench
    # Old next had no generation flags. Its UI sends confirm=true for already
    # confirmed items; a header edit must not add a new semantic field to them.
    with sessions() as session:
        resume = session.scalar(select(Resume).where(Resume.batch_id == identifier, Resume.kind == "job"))
        items = deepcopy(resume.items)
        for item in items:
            item.pop("flags", None)
            item["source_status"] = item["verification_status"] = "user_confirmed"
        resume.items = items
        resume.semantic_hash = semantic_hash({"title":resume.title,"summary":resume.summary,"items":items}, resume.plans, JD)
        session.commit()
    retry_guide(sessions, identifier, "job")
    assert process_guide_once(sessions, LocalProvider())
    current = get_batch(sessions, identifier)["resumes"]["job"]
    ui_items = [{"id":item["id"],"text":item["text"],"locked":False,"confirm":True,"achieved":item["achieved"]} for item in current["items"]]
    response = client.put(f"/api/batches/{identifier}/resumes/job", json=body(current, header={}, items=ui_items))
    assert response.status_code == 200
    state = response.json()
    assert state["resumes"]["job"]["semantic_hash"] == current["semantic_hash"]
    assert "flags" not in state["resumes"]["job"]["items"][0]
    assert state["guides"]["job"]["status"] == "ready"
