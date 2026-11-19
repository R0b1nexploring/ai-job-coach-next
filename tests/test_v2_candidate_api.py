from fastapi.testclient import TestClient
from io import BytesIO
from hashlib import sha256
import json
import sys
from zipfile import ZipFile

from app.api import app


def test_candidates_api_is_ephemeral_and_returns_exact_unicode_spans():
    client = TestClient(app)
    response = client.post("/api/v2/fact-candidates", json={"text": "  课程😀\r\n完成记录\n"})
    assert response.status_code == 200
    result = response.json()
    assert result["normalized_text"] == "  课程😀\n完成记录\n"
    assert [row["value"] for row in result["candidates"]] == ["课程😀", "完成记录"]
    assert all(row["confirmation"] == "unconfirmed" for row in result["candidates"])
    for row in result["candidates"]:
        assert result["normalized_text"][row["start"]:row["end"]] == row["value"]


def test_candidates_api_respects_browser_origin_and_limits():
    client = TestClient(app)
    assert client.post("/api/v2/fact-candidates", json={"text": "x" * 20001}).status_code == 422
    assert client.post("/api/v2/fact-candidates", headers={"origin": "https://other.example"}, json={"text": "x"}).status_code == 403


def test_guide_locale_defaults_to_resume_locale(tmp_path, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    from app import api
    from app.db import Base, make_engine
    monkeypatch.setenv("COACH_V2_ENABLED", "1")
    monkeypatch.setenv("COACH_PROVIDER", "local")
    engine = make_engine(f"sqlite:///{tmp_path / 'locale.sqlite'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    client = TestClient(app)
    response = client.post("/api/v2/batches", headers={"Idempotency-Key": "locale-default"}, json={
        "jd_text": "虚构岗位负责工程记录整理、资料核对及相关协作工作。", "material_text": "", "resume_locale": "en"})
    assert response.status_code == 202
    view = client.get(f'/api/v2/batches/{response.json()["id"]}').json()
    assert view["resume_locale"] == view["guide_locale"] == "en"
    engine.dispose()


def test_reused_material_retirement_keeps_selected_batch_evidence(tmp_path, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    from app import api
    from app.db import Base, make_engine
    monkeypatch.setenv("COACH_V2_ENABLED", "1")
    monkeypatch.setenv("COACH_PROVIDER", "local")
    engine = make_engine(f"sqlite:///{tmp_path / 'material-flow.sqlite'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(api, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    client = TestClient(app)
    source = "虚构实验设备记录"
    material = client.post("/api/v2/materials", json={"extracted_text": source, "source_type": "manual"})
    assert material.status_code == 201
    row = material.json()
    fact = client.post("/api/v2/fact-candidates", json={"text": source}).json()["candidates"][0]
    fact["fact_type"] = "action"
    payload = {"jd_text": "虚构岗位负责工程记录整理、资料核对及相关协作工作。", "material_text": source,
               "candidates": [fact], "material_revision_id": row["revision_id"], "source_type": "manual"}
    created = client.post("/api/v2/batches", headers={"Idempotency-Key": "fictional-reuse"}, json=payload)
    assert created.status_code == 202
    batch_id = created.json()["id"]
    preview = client.get(f'/api/v2/materials/{row["material_id"]}/deletion-preview?mode=preserve_existing').json()
    assert batch_id in preview["affected_batch_ids"]
    removed = client.request("DELETE", f'/api/v2/materials/{row["material_id"]}', json={
        "mode": "preserve_existing", "impact_token": preview["impact_token"]})
    assert removed.status_code == 204
    assert client.get(f'/api/v2/materials/{row["material_id"]}/revisions/{row["revision_id"]}').status_code == 404
    saved = client.get(f"/api/v2/batches/{batch_id}").json()
    assert saved["material_sources"][0]["extracted_text"] == source
    blocked = client.post("/api/v2/batches", headers={"Idempotency-Key": "fictional-reuse-again"}, json=payload)
    assert blocked.status_code in {404, 409, 422}
    engine.dispose()


def test_v2_api_complete_draft_flow_and_deletion(tmp_path, monkeypatch):
    from sqlalchemy import select
    from sqlalchemy.orm import sessionmaker
    from app import api
    from app.db import Base, make_engine
    from app.models import FactSnapshot, MaterialRevision
    from app.worker_v2 import process_batch_v2_once, process_guide_v2_once

    monkeypatch.setenv("COACH_V2_ENABLED", "1")
    monkeypatch.setenv("COACH_PROVIDER", "local")
    engine = make_engine(f"sqlite:///{tmp_path / 'http-v2.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(api, "SessionLocal", sessions)
    client = TestClient(app)
    source = "虚构设备记录"
    candidates = client.post("/api/v2/fact-candidates", json={"text": source}).json()["candidates"]
    preview = client.post("/api/v2/projection-preview", json={"candidates": candidates, "header": {}})
    assert preview.status_code == 200 and preview.json()["excluded"][0]["reason"] == "unsupported_type"
    malformed = [{**candidates[0], "start": True}]
    assert client.post("/api/v2/projection-preview", json={"candidates": malformed, "header": {}}).status_code == 422
    candidates[0]["fact_type"] = "action"
    assert client.post("/api/v2/projection-preview", json={"candidates": candidates, "header": {}}).json()["excluded"] == []
    response = client.post("/api/v2/batches", headers={"Idempotency-Key": "fictional-http-v2"}, json={
        "jd_text": "虚构岗位负责工程记录整理、资料核对及相关协作工作。", "material_text": source,
        "candidates": candidates, "header": {}, "resume_locale": "zh-CN", "guide_locale": "en"})
    assert response.status_code == 202
    batch_id = response.json()["id"]
    pending = client.get(f"/api/v2/batches/{batch_id}/package?page_target=2")
    assert pending.status_code == 409 and pending.json()["missing"]
    assert process_batch_v2_once(sessions)
    assert process_guide_v2_once(sessions) and process_guide_v2_once(sessions)
    detail = client.get(f"/api/v2/batches/{batch_id}")
    assert detail.status_code == 200 and detail.json()["review"]["status"] == "unavailable"
    assert client.get("/api/v2/batches").json()[0]["id"] == batch_id
    assert client.get(f"/api/v2/batches/{batch_id}/history/job").json()["resumes"][0]["revision"] == 1
    assert detail.json()["guides"]["job"]["status"] == "ready"
    for kind in ("job", "blueprint"):
        for document in ("resume", "guide"):
            for fmt in ("html", "docx"):
                response = client.get(f"/api/v2/batches/{batch_id}/documents/{kind}/{document}/{fmt}")
                assert response.status_code == 200
                assert response.headers["x-document-status"] == "draft"
                assert response.content
            response = client.get(f"/api/v2/batches/{batch_id}/documents/{kind}/{document}/pdf?page_target=2")
            if sys.platform == "darwin":
                from pypdf import PdfReader
                assert response.status_code == 200
                assert response.headers["x-document-status"] == "draft"
                assert len(PdfReader(BytesIO(response.content)).pages) in (1, 2)
                assert "".join(page.extract_text() for page in PdfReader(BytesIO(response.content)).pages).strip()
            else:
                assert response.status_code == 503  # no silent fallback to another renderer
    package = client.get(f"/api/v2/batches/{batch_id}/package?page_target=2")
    if sys.platform == "darwin":
        assert package.status_code == 200
        with ZipFile(BytesIO(package.content)) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            assert len(manifest["files"]) == 8
            assert {(record["kind"], record["type"], record["format"]) for record in manifest["files"]} == {
                (kind, document, fmt) for kind in ("job", "blueprint") for document in ("resume", "guide") for fmt in ("docx", "pdf")}
            for record in manifest["files"]:
                assert sha256(archive.read(record["file"])).hexdigest() == record["sha256"]
                assert record["purpose"] == "draft" and record["file_qa"] == "not_checked"
    else:
        assert package.status_code == 503
    blueprint = detail.json()["resumes"]["blueprint"]
    plan = blueprint["plans"][0]
    updated = client.put(f"/api/v2/batches/{batch_id}/resumes/blueprint", json={
        "expected_revision": blueprint["revision"], "title": blueprint["document"]["title"],
        "unit_texts": {}, "plan_updates": {plan["key"]: {"background": "用户修改的虚构背景"}}})
    assert updated.status_code == 200 and updated.json()["revision"] == 2
    refreshed = client.get(f"/api/v2/batches/{batch_id}").json()
    assert refreshed["resumes"]["blueprint"]["plans"][0]["background"] == "用户修改的虚构背景"
    assert "background" in refreshed["resumes"]["blueprint"]["plans"][0]["needs_review"]
    assert refreshed["guides"]["blueprint"]["status"] == "stale"
    assert refreshed["guides"]["job"]["status"] == "ready"
    monkeypatch.setenv("COACH_V2_ENABLED", "0")
    assert client.get("/api/v2/batches").json()[0]["id"] == batch_id
    assert client.get(f"/api/v2/batches/{batch_id}").status_code == 200
    assert client.post(f"/api/v2/batches/{batch_id}/guides/blueprint/retry").status_code == 503
    assert client.delete(f"/api/v2/batches/{batch_id}").status_code == 204
    assert client.get(f"/api/v2/batches/{batch_id}").status_code == 404
    assert client.get("/api/v2/batches").json() == []
    with sessions() as session:
        assert not session.scalars(select(MaterialRevision)).all()
        assert not session.scalars(select(FactSnapshot)).all()
    engine.dispose()


def test_projection_preview_uses_same_header_normalization_as_batch():
    client = TestClient(app)
    candidate = {"fact_type": "action", "value": "曾在虚构学校\n某专业学习", "start": 0,
                 "end": 1, "quote_hash": "a" * 64}
    response = client.post('/api/v2/projection-preview', json={"candidates": [candidate],
        "header": {"education": "虚构学校\r\n某专业"}})
    assert response.status_code == 200
    assert response.json()["excluded"] == [{"ordinal": 0, "reason": "sensitive_value"}]


def test_projection_preview_matches_created_snapshot_with_ca_header(tmp_path, monkeypatch):
    from sqlalchemy.orm import sessionmaker
    from app import api
    from app.db import Base, make_engine
    from app.models import Batch, FactSnapshot
    from app.privacy_v2 import v2_write_transaction
    from app.service_v2 import load_fact_snapshot
    from app.model_projection_v2 import project_facts_with_diagnostics
    monkeypatch.setenv("COACH_V2_ENABLED", "1")
    monkeypatch.setenv("COACH_PROVIDER", "local")
    engine = make_engine(f"sqlite:///{tmp_path / 'projection-match.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(api, "SessionLocal", sessions)
    client = TestClient(app)
    source = "虚构设备记录"
    candidate = client.post("/api/v2/fact-candidates", json={"text": source}).json()["candidates"][0]
    candidate["fact_type"] = "action"
    header = {"location": "CA"}
    preview = client.post("/api/v2/projection-preview", json={"candidates": [candidate], "header": header})
    assert preview.status_code == 200
    created = client.post("/api/v2/batches", headers={"Idempotency-Key": "preview-match-ca"}, json={
        "jd_text": "虚构岗位负责工程记录整理、资料核对及相关协作工作。",
        "material_text": source, "candidates": [candidate], "header": header})
    assert created.status_code == 202
    batch_id = created.json()["id"]
    with v2_write_transaction(sessions, batch_id=batch_id) as session:
        batch = session.get(Batch, batch_id)
        snapshot = load_fact_snapshot(session.get(FactSnapshot, batch.fact_snapshot_id))
        actual = project_facts_with_diagnostics(snapshot, private_header=header, alias_identifiers=True)
        assert preview.json()["excluded"] == [{"ordinal": d["ordinal"], "reason": d["reason"]} for d in actual.diagnostics.excluded]
        assert len(actual.model_input["claims"]) == 1
    engine.dispose()
