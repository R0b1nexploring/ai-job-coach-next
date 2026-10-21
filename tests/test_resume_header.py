"""Fictional header edits stay versioned and outside project interview prompts."""
from copy import deepcopy
import os
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from app import api
from app.db import Base, make_engine
from app.providers import LocalProvider
from app.service import (
    InputConflict, create_batch, get_batch, get_guide_revision,
    get_resume_revision, process_batch_once, process_guide_once,
    retry_guide, save_resume,
)

JD = "虚构机械岗位：负责结构设计、装配图输出与方案验证。"
MATERIAL = "虚构课程项目：绘制装配图并整理零件清单，进行方案评审。"
HEADER = {
    "name": "虚构姓名",
    "phone": "000-0000-0000",
    "email": "fictional@example.invalid",
    "location": "虚构城市",
    "education": "虚构大学 · 机械工程 · 本科\n2022–2026 · 虚构课程学习",
}


@pytest.fixture
def sessions(tmp_path):
    url = os.getenv("COACH_PG_TEST_URL")
    schema = None
    if url:
        engine = create_engine(url, pool_pre_ping=True)
        schema = "header_" + uuid4().hex
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped = engine.execution_options(schema_translate_map={None: schema})
    else:
        engine = make_engine(f"sqlite:///{tmp_path / 'header.sqlite'}")
        scoped = engine
    Base.metadata.create_all(scoped)
    try:
        yield sessionmaker(bind=scoped, expire_on_commit=False)
    finally:
        if schema:
            with engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def generated(sessions, *, ready=True, provider=None):
    batch = create_batch(sessions, jd=JD, material=MATERIAL, confirmed=False, idempotency_key="fictional-header")
    assert process_batch_once(sessions, provider or LocalProvider())
    if ready:
        assert process_guide_once(sessions, LocalProvider())
        assert process_guide_once(sessions, LocalProvider())
    return batch.id


def edit(sessions, batch_id, kind="job", **changes):
    resume = get_batch(sessions, batch_id)["resumes"][kind]
    arguments = {key: deepcopy(resume[key]) for key in ("revision", "title", "summary", "items", "plans")}
    arguments.update(changes)
    return save_resume(sessions, batch_id, kind, **arguments)


def test_generated_resume_ignores_model_identity_fields(sessions):
    class FabricatedHeader(LocalProvider):
        def bundle(self, jd, material):
            bundle = super().bundle(jd, material)
            for kind in ("job", "blueprint"):
                bundle[kind]["header"] = deepcopy(HEADER)
            return bundle

    batch_id = generated(sessions, provider=FabricatedHeader())
    batch = get_batch(sessions, batch_id)
    for kind in ("job", "blueprint"):
        assert batch["resumes"][kind]["header"] == {}
        assert get_resume_revision(sessions, batch_id, kind, 1)["header"] == {}


def test_header_edits_are_independent_and_preserve_guides_and_history(sessions):
    batch_id = generated(sessions)
    before = get_batch(sessions, batch_id)
    old_guide = get_guide_revision(sessions, batch_id, "job", 1)
    saved = edit(sessions, batch_id, header=HEADER)
    resume = saved["resumes"]["job"]
    assert resume["header"] == HEADER
    assert resume["revision"] == 2
    assert resume["content_hash"] != before["resumes"]["job"]["content_hash"]
    assert resume["semantic_hash"] == before["resumes"]["job"]["semantic_hash"]
    assert resume["items"] == before["resumes"]["job"]["items"]
    assert saved["resumes"]["blueprint"] == before["resumes"]["blueprint"]
    assert saved["guides"]["job"]["status"] == "ready"
    assert saved["guides"]["job"]["resume_revision"] == 1
    assert saved["guides"]["job"]["applies_to_revision"] == 2
    assert get_guide_revision(sessions, batch_id, "job", 1) == old_guide
    assert get_resume_revision(sessions, batch_id, "job", 1)["header"] == {}
    assert get_resume_revision(sessions, batch_id, "job", 2)["header"] == HEADER
    with pytest.raises(InputConflict):
        edit(sessions, batch_id, revision=1, header={"name": "另一个虚构姓名"})
    assert get_batch(sessions, batch_id)["resumes"]["job"]["header"] == HEADER


def test_header_omission_and_null_preserve_values_and_empty_object_clears(sessions):
    batch_id = generated(sessions)
    before = get_batch(sessions, batch_id)["resumes"]["job"]
    assert edit(sessions, batch_id, header={})["resumes"]["job"]["revision"] == 1
    saved = edit(sessions, batch_id, header=HEADER)
    assert edit(sessions, batch_id)["resumes"]["job"] == saved["resumes"]["job"]
    assert edit(sessions, batch_id, header=None)["resumes"]["job"] == saved["resumes"]["job"]
    cleared = edit(sessions, batch_id, header={})["resumes"]["job"]
    assert cleared["header"] == {}
    assert cleared["revision"] == 3
    assert cleared["content_hash"] == before["content_hash"]
    assert cleared["semantic_hash"] == before["semantic_hash"]


def test_header_normalization_drops_empty_fields_and_preserves_education_lines(sessions):
    batch_id = generated(sessions)
    result = edit(sessions, batch_id, header={"name": "  虚构姓名  ", "phone": "  ", "email": "", "education": "  虚构大学\r\n机械工程\r本科  "})
    assert result["resumes"]["job"]["header"] == {"name": "虚构姓名", "education": "虚构大学\n机械工程\n本科"}


@pytest.mark.parametrize("header", [
    {"unknown": "虚构字段"}, {"name": 123}, {"phone": False}, {"education": ["虚构大学"]},
    {"name": None}, {"name": "虚构\n姓名"}, {"phone": "000\r000"},
    {"email": "fictional@example.invalid\n"}, {"location": "虚构\r\n城市"},
    {"name": "字" * 81}, {"phone": "0" * 41}, {"email": "a" * 161},
    {"location": "字" * 81}, {"education": "字" * 1501},
])
def test_invalid_header_is_rejected_without_changing_revision(sessions, header):
    batch_id = generated(sessions)
    before = get_batch(sessions, batch_id)
    with pytest.raises(ValueError):
        edit(sessions, batch_id, header=header)
    assert get_batch(sessions, batch_id) == before


def test_api_accepts_versioned_header_and_rejects_unknown_or_nonstring_values(sessions, monkeypatch):
    batch_id = generated(sessions)
    monkeypatch.setattr(api, "SessionLocal", sessions)
    client = TestClient(api.app)
    route = f"/api/batches/{batch_id}/resumes/job"
    resume = client.get(f"/api/batches/{batch_id}").json()["resumes"]["job"]
    body = {key: deepcopy(resume[key]) for key in ("revision", "title", "summary", "items", "plans")}
    for invalid in ({"unknown": "虚构字段"}, {"name": 123}, {"email": "虚构\n邮箱"}, ["虚构姓名"]):
        assert client.put(route, json={**body, "header": invalid}).status_code == 422
    saved = client.put(route, json={**body, "header": HEADER})
    assert saved.status_code == 200
    assert saved.json()["resumes"]["job"]["header"] == HEADER
    body["revision"] = 2
    assert client.put(route, json=body).json()["resumes"]["job"]["header"] == HEADER
    assert client.put(route, json={**body, "header": None}).json()["resumes"]["job"]["header"] == HEADER
    history = client.get(f"/api/batches/{batch_id}/resumes/job/revisions").json()
    assert history[0]["header"] == HEADER and history[1]["header"] == {}
    assert client.put(route, json={**body, "header": {}}).json()["resumes"]["job"]["header"] == {}


def test_guide_generation_never_receives_header_fields(sessions):
    batch_id = generated(sessions)
    edit(sessions, batch_id, header=HEADER)
    retry_guide(sessions, batch_id, "job")
    observed = []

    class Inspect(LocalProvider):
        def guide(self, kind, resume, plans, jd):
            observed.append(deepcopy(resume))
            assert set(resume) == {"title", "summary", "items"}
            assert "fictional@example.invalid" not in str(resume)
            return super().guide(kind, resume, plans, jd)

    assert process_guide_once(sessions, Inspect())
    assert len(observed) == 1
    assert get_batch(sessions, batch_id)["guides"]["job"]["status"] == "ready"
    assert get_guide_revision(sessions, batch_id, "job", 2)["resume"]["header"] == HEADER


def test_header_only_edit_during_guide_generation_keeps_original_binding(sessions):
    batch_id = generated(sessions, ready=False)
    changed_kind = []

    class HeaderEdit(LocalProvider):
        def guide(self, kind, resume, plans, jd):
            changed_kind.append(kind)
            edit(sessions, batch_id, kind, header=HEADER)
            return super().guide(kind, resume, plans, jd)

    assert process_guide_once(sessions, HeaderEdit())
    kind = changed_kind[0]
    result = get_batch(sessions, batch_id)
    assert result["resumes"][kind]["revision"] == 2
    assert result["guides"][kind]["status"] == "ready"
    assert result["guides"][kind]["resume_revision"] == 1
    assert result["guides"][kind]["applies_to_revision"] == 2
    assert get_guide_revision(sessions, batch_id, kind, 1)["resume"]["header"] == {}
