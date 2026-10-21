from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import api
from app.db import Base
from app.providers import LocalProvider
from app.service import process_batch_once, process_guide_once


def test_browser_api_survives_reopen_and_returns_both_guides(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'api.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(api, "SessionLocal", sessions)
    client = TestClient(api.app)
    response = client.post("/api/batches", headers={"Idempotency-Key": "browser-retry-01"}, json={"jd_text": "机械结构设计工程师，负责装配设计与工程图输出。", "material_text": "", "material_confirmed": False})
    assert response.status_code == 202
    batch_id = response.json()["id"]
    assert client.get(f"/api/batches/{batch_id}").json()["status"] == "queued"
    provider = LocalProvider()
    assert process_batch_once(sessions, provider)
    assert process_guide_once(sessions, provider)
    assert process_guide_once(sessions, provider)
    reopened = client.get("/api/batches/latest")
    assert reopened.status_code == 200
    data = reopened.json()
    assert data["id"] == batch_id
    assert data["status"] == "ready"
    assert set(data["resumes"]) == {"job", "blueprint"}
    assert set(data["guides"]) == {"job", "blueprint"}
    assert client.get("/").status_code == 200
    engine.dispose()


def test_upload_edit_refresh_and_download(tmp_path, monkeypatch):
    from io import BytesIO
    from docx import Document
    from pypdf import PdfReader

    engine = create_engine(f"sqlite:///{tmp_path / 'delivery.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(api, "SessionLocal", sessions)
    client = TestClient(api.app)
    upload = client.post("/api/materials/extract", files={"file": ("sample.txt", "虚构课程项目：绘制装配图并整理零部件清单。".encode(), "text/plain")})
    assert upload.status_code == 200
    created = client.post("/api/batches", headers={"Idempotency-Key": "delivery-api-01"}, json={"jd_text": "机械设计工程师负责结构设计和装配图输出。", "material_text": upload.json()["text"], "material_confirmed": False})
    batch_id = created.json()["id"]
    provider = LocalProvider()
    assert process_batch_once(sessions, provider)
    assert process_guide_once(sessions, provider)
    assert process_guide_once(sessions, provider)
    initial = client.get(f"/api/batches/{batch_id}").json()
    job = initial["resumes"]["job"]
    job["items"][0]["text"] = "本人整理装配图和零部件清单，形成可讨论的设计材料。"
    saved = client.put(f"/api/batches/{batch_id}/resumes/job", json={"revision": 1, "title": job["title"], "summary": job["summary"], "items": job["items"]})
    assert saved.status_code == 200
    assert saved.json()["guides"]["job"]["status"] == "stale"
    assert client.post(f"/api/batches/{batch_id}/guides/job/retry").status_code == 202
    assert process_guide_once(sessions, provider)
    assert client.get(f"/api/batches/{batch_id}").json()["guides"]["job"]["status"] == "ready"
    docx = client.get(f"/api/batches/{batch_id}/resumes/job/download/docx")
    pdf = client.get(f"/api/batches/{batch_id}/resumes/job/download/pdf")
    assert docx.status_code == pdf.status_code == 200
    assert "本人整理装配图" in "\n".join(p.text for p in Document(BytesIO(docx.content)).paragraphs)
    assert PdfReader(BytesIO(pdf.content)).pages
    engine.dispose()
