"""Initial profile seed: validation, generation boundary, and exports.

All data is fictional. The initial profile must never reach the model; generated
resumes own independent header snapshots and are edited through saved revisions.
"""
from __future__ import annotations

from io import BytesIO

from docx import Document
from fastapi.testclient import TestClient
from pypdf import PdfReader
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import api
from app.db import Base
from app.providers import LocalProvider
from app.service import InputConflict, create_batch, get_batch, process_batch_once, process_guide_once, update_profile, validate_profile

JD = "机械结构设计工程师\n负责装配结构设计、图纸表达和可制造性评审。\n要求说明设计约束和验证方法。"
MATERIAL = "毕业项目：绘制装配图并整理零部件清单，完成课程设计汇报。"
PROFILE = {"name": "王小明", "phone": "13800000000", "email": "fictional@example.com", "education": "虚构大学 机械工程 本科 2026 届"}


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'profile.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def test_validate_profile_strips_and_omits_empty_fields():
    assert validate_profile(None) == {}
    assert validate_profile({"name": "  王小明 ", "phone": "", "email": None}) == {"name": "王小明"}
    with pytest.raises(ValueError):
        validate_profile({"name": "x" * 81})
    with pytest.raises(ValueError):
        validate_profile({"company": "不在允许字段内"})
    with pytest.raises(ValueError):
        validate_profile({"name": 123})
    with pytest.raises(ValueError):
        validate_profile("not a dict")


def test_profile_joins_batch_and_conflicting_profile_conflicts(sessions):
    batch = create_batch(sessions, jd=JD, material=MATERIAL, confirmed=True, idempotency_key="profile-case", profile=PROFILE)
    same = create_batch(sessions, jd=JD, material=MATERIAL, confirmed=True, idempotency_key="profile-case", profile=dict(PROFILE))
    assert same.id == batch.id
    with pytest.raises(InputConflict):
        create_batch(sessions, jd=JD, material=MATERIAL, confirmed=True, idempotency_key="profile-case", profile={"name": "别人"})
    assert get_batch(sessions, batch.id)["profile"] == PROFILE


def test_profile_can_only_edit_the_seed_before_generation(sessions):
    batch = create_batch(sessions, jd=JD, material=MATERIAL, confirmed=True, idempotency_key="profile-edit", profile=PROFILE)
    updated_profile = {"name": "王小明", "phone": "13900000000"}
    assert update_profile(sessions, batch.id, updated_profile)["profile"] == updated_profile
    provider = LocalProvider()
    assert process_batch_once(sessions, provider)
    assert process_guide_once(sessions, provider)
    assert process_guide_once(sessions, provider)
    before = get_batch(sessions, batch.id)
    assert before["resumes"]["job"]["header"] == updated_profile
    assert before["resumes"]["blueprint"]["header"] == updated_profile
    with pytest.raises(InputConflict):
        update_profile(sessions, batch.id, {"name": "别人"})
    assert get_batch(sessions, batch.id) == before
    with pytest.raises(ValueError):
        update_profile(sessions, batch.id, {"name": "x" * 81})


def test_legacy_profile_render_argument_remains_supported():
    from app.files import render_docx, render_html, render_pdf

    resume = {"title": "机械结构设计工程师求职简历", "summary": "虚构简介，仅用于导出测试。", "items": [{"text": "虚构项目要点，仅用于导出测试。"}]}
    html = render_html(resume, profile=PROFILE).decode()
    assert all(text in html for text in ("王小明", "13800000000", "fictional@example.com", "教育背景", "虚构大学"))

    partial = render_html(resume, profile={"education": "虚构大学 本科"}).decode()
    assert "教育背景" in partial
    assert "电话" in partial and "邮箱" in partial

    paragraphs = [p.text for p in Document(BytesIO(render_docx(resume, profile=PROFILE))).paragraphs]
    assert "王小明" in "\n".join(paragraphs)
    assert all(text in "\n".join(paragraphs) for text in ("13800000000", "fictional@example.com", "教育背景", "虚构大学 机械工程 本科 2026 届"))

    pdf = PdfReader(BytesIO(render_pdf(resume, profile=PROFILE)))
    extracted = "".join(page.extract_text() for page in pdf.pages).replace("\n", "")
    assert "王小明" in extracted and "13800000000" in extracted and "虚构大学" in extracted

    # Missing information leaves space for the user to fill.
    plain = render_html(resume).decode()
    assert "教育背景" in plain and "姓名" in plain and "None" not in plain


def test_profile_api_roundtrip_and_pdf_download(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'profile.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(api, "SessionLocal", sessions)
    client = TestClient(api.app)
    created = client.post("/api/batches", headers={"Idempotency-Key": "profile-api-01"}, json={"jd_text": JD, "material_text": MATERIAL, "material_confirmed": True, "profile": PROFILE})
    assert created.status_code == 202
    batch_id = created.json()["id"]
    provider = LocalProvider()
    assert process_batch_once(sessions, provider)
    assert process_guide_once(sessions, provider)
    assert process_guide_once(sessions, provider)

    assert client.get(f"/api/batches/{batch_id}").json()["profile"] == PROFILE
    assert client.patch(f"/api/batches/{batch_id}/profile", json={"profile": {"name": "别人"}}).status_code == 409
    resume = client.get(f"/api/batches/{batch_id}").json()["resumes"]["job"]
    body = {field: resume[field] for field in ("revision", "title", "summary", "items", "plans")}
    updated = client.put(f"/api/batches/{batch_id}/resumes/job", json={**body, "header": {"name": "王小明", "phone": "13900000000"}})
    assert updated.status_code == 200
    assert updated.json()["resumes"]["job"]["header"] == {"name": "王小明", "phone": "13900000000"}
    assert updated.json()["resumes"]["blueprint"]["header"] == PROFILE
    assert updated.json()["guides"]["job"]["status"] == "ready"
    assert client.patch(f"/api/batches/{batch_id}/profile", json={"profile": {"unknown": "字段"}}).status_code == 422
    assert client.patch(f"/api/batches/{'0' * 32}/profile", json={"profile": {}}).status_code == 404

    pdf = client.get(f"/api/batches/{batch_id}/resumes/job/download/pdf")
    assert pdf.status_code == 200
    extracted = "".join(page.extract_text() for page in PdfReader(BytesIO(pdf.content)).pages).replace("\n", "")
    assert "王小明" in extracted and "13900000000" in extracted and "虚构大学" not in extracted
    engine.dispose()
