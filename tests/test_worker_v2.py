from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.generation_v2 import LocalWriterV2
from app.models import Batch, FactSnapshot, Guide, GuideRevision, Resume, ResumeRevision, TaskExecution, utcnow
from app.privacy_v2 import V2Deleted, delete_v2_batch, v2_write_transaction
from app.service_v2 import create_v2_batch, export_v2_document
from app.service import NotFound
from app.worker_v2 import _claim_batch, process_batch_v2_once, process_guide_v2_once

JD = "虚构设备工程岗位：负责设备记录、装配设计与方案验证。"


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("COACH_PROVIDER", "local")
    engine = create_engine(f"sqlite:///{tmp_path / 'worker.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def create(sessions, key="batch-001"):
    return create_v2_batch(sessions, jd=JD, material="", candidates=[], header={}, resume_locale="zh-CN", guide_locale="en", idempotency_key=key)["id"]


def test_worker_saves_both_versions_and_guides_as_unreviewed_drafts(sessions):
    batch_id = create(sessions)
    assert process_batch_v2_once(sessions)
    with v2_write_transaction(sessions, batch_id=batch_id) as session:
        batch = session.get(Batch, batch_id)
        assert batch.status == "guides_pending" and batch.plans["v2_review"]["status"] == "unavailable"
        resumes = session.scalars(select(Resume).where(Resume.batch_id == batch_id)).all()
        assert {row.kind for row in resumes} == {"job", "blueprint"}
        assert all(row.schema_version == 2 and row.items == [] and row.document_v2["schema_version"] == 2 for row in resumes)
        assert len(session.scalars(select(GuideRevision).where(GuideRevision.batch_id == batch_id)).all()) == 2
    assert process_guide_v2_once(sessions)
    assert process_guide_v2_once(sessions)
    assert not process_guide_v2_once(sessions)
    with v2_write_transaction(sessions, batch_id=batch_id) as session:
        batch = session.get(Batch, batch_id)
        assert batch.status == "ready" and batch.plans["v2_review"]["status"] == "unavailable"
        for guide in session.scalars(select(Guide).where(Guide.batch_id == batch_id)):
            assert guide.status == "ready" and guide.sections["locale"] == "en"
            assert guide.sections["resume_id"] == guide.resume_id
            assert guide.sections["review_status"] == "not_run"


def test_only_one_worker_claims_batch(sessions):
    batch_id = create(sessions)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: _claim_batch(sessions), range(2)))
    assert len([claim for claim in claims if claim]) == 1
    with v2_write_transaction(sessions, batch_id=batch_id) as session:
        assert session.get(Batch, batch_id).attempts == 1
        assert len(session.scalars(select(TaskExecution).where(TaskExecution.batch_id == batch_id)).all()) == 1


def test_review_error_blocks_both_documents_and_guide_creation(sessions):
    batch_id = create(sessions)
    class Blocking(LocalWriterV2):
        name = "fictional"
        def review(self, payload):
            target = payload["documents"]["job"]["summary"]["unit_id"]
            return {"issues": [{"severity": "error", "scope": "job", "target_id": target, "diagnosis": "虚构问题", "recommendation": "核对事实"}]}
    assert process_batch_v2_once(sessions, lambda _: Blocking())
    with v2_write_transaction(sessions, batch_id=batch_id) as session:
        batch = session.get(Batch, batch_id)
        assert batch.status == "review_blocked"
        assert batch.plans["v2_review"]["status"] == "blocked" and batch.plans["candidate"]
        issue = batch.plans["v2_review"]["issues"][0]
        assert batch.plans["diagnostic_documents"]["job"]["summary"]["unit_id"] == issue["target_id"]
        assert batch.plans["id_map"] and batch.plans["projects"]
        assert not session.scalars(select(Resume).where(Resume.batch_id == batch_id)).all()
        assert not session.scalars(select(Guide).where(Guide.batch_id == batch_id)).all()
    with pytest.raises(NotFound):
        export_v2_document(sessions, batch_id=batch_id, kind="job", document_type="resume", file_format="html")


@pytest.mark.parametrize("action", ["expire", "delete"])
@pytest.mark.parametrize("fail", [False, True])
def test_late_batch_result_or_failure_cannot_publish(sessions, action, fail):
    batch_id = create(sessions)
    class Late(LocalWriterV2):
        def bundle(self, request):
            output = super().bundle(request)
            if action == "delete":
                delete_v2_batch(sessions, batch_id=batch_id)
            else:
                with v2_write_transaction(sessions, batch_id=batch_id) as session:
                    session.get(Batch, batch_id).lease_until = utcnow() - timedelta(seconds=1)
            if fail:
                raise RuntimeError("fictional failure")
            return output
    assert process_batch_v2_once(sessions, lambda _: Late())
    if action == "delete":
        with pytest.raises(V2Deleted):
            with v2_write_transaction(sessions, batch_id=batch_id): pass
        with sessions() as session:
            assert session.get(Batch, batch_id) is None
            assert not session.scalars(select(Resume).where(Resume.batch_id == batch_id)).all()
    else:
        with v2_write_transaction(sessions, batch_id=batch_id) as session:
            batch = session.get(Batch, batch_id)
            assert batch.status == "running" and batch.error_code is None
            assert not session.scalars(select(Resume).where(Resume.batch_id == batch_id)).all()


@pytest.mark.parametrize("action", ["expire", "delete", "edit"])
@pytest.mark.parametrize("fail", [False, True])
def test_late_guide_result_is_never_published(sessions, action, fail):
    batch_id = create(sessions)
    assert process_batch_v2_once(sessions)
    class Late(LocalWriterV2):
        def guide(self, request):
            output = super().guide(request)
            if action == "delete":
                delete_v2_batch(sessions, batch_id=batch_id)
            else:
                with v2_write_transaction(sessions, batch_id=batch_id) as session:
                    guide = session.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == request.kind))
                    if action == "expire": guide.lease_until = utcnow() - timedelta(seconds=1)
                    else:
                        resume = session.get(Resume, guide.resume_id)
                        resume.revision += 1
                        resume.semantic_hash = "0" * 64
            if fail:
                raise RuntimeError("fictional failure")
            return output
    assert process_guide_v2_once(sessions, lambda _: Late())
    if action == "delete":
        with pytest.raises(V2Deleted):
            with v2_write_transaction(sessions, batch_id=batch_id): pass
    else:
        with v2_write_transaction(sessions, batch_id=batch_id) as session:
            guides = session.scalars(select(Guide).where(Guide.batch_id == batch_id)).all()
            assert all(guide.sections is None for guide in guides)


def update_header_only(sessions, batch_id, guide_kind):
    with v2_write_transaction(sessions, batch_id=batch_id) as session:
        guide = session.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == guide_kind))
        resume = session.get(Resume, guide.resume_id)
        resume.revision += 1
        resume.header = {"name": "虚构姓名"}
        session.add(ResumeRevision(resume_id=resume.id, revision=resume.revision, title=resume.title, summary=resume.summary,
            header=resume.header, items=[], content_hash=resume.content_hash, semantic_hash=resume.semantic_hash,
            plans=resume.plans, schema_version=2, document_v2=resume.document_v2))


@pytest.mark.parametrize("during_call", [False, True])
def test_header_only_new_revision_keeps_guide_bound_to_original(sessions, during_call):
    batch_id = create(sessions)
    assert process_batch_v2_once(sessions)
    # Guide selection orders blueprint before job.
    if not during_call:
        update_header_only(sessions, batch_id, "blueprint")
    class Writer(LocalWriterV2):
        def guide(self, request):
            if during_call:
                update_header_only(sessions, batch_id, request.kind)
            return super().guide(request)
    assert process_guide_v2_once(sessions, lambda _: Writer())
    with v2_write_transaction(sessions, batch_id=batch_id) as session:
        guide = session.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == "blueprint"))
        assert session.get(Resume, guide.resume_id).revision == 2
        assert guide.status == "ready" and guide.resume_revision == 1
        assert guide.sections["resume_revision"] == 1


def test_material_fact_flows_through_bundle_and_saved_snapshot(sessions):
    from app.content_v2 import text_hash
    material = "虚构课程项目：整理设备记录。"
    value = "整理设备记录"
    start = material.index(value)
    batch_id = create_v2_batch(sessions, jd=JD, material=material,
        candidates=[{"start": start, "end": start + len(value), "quote_hash": text_hash(value),
                     "fact_type": "action", "value": value, "confirmed": False}],
        header={}, resume_locale="zh-CN", guide_locale="zh-CN", idempotency_key="material-test")["id"]
    assert process_batch_v2_once(sessions)
    with v2_write_transaction(sessions, batch_id=batch_id) as session:
        batch = session.get(Batch, batch_id)
        assert batch.status == "guides_pending"
        fact = session.get(FactSnapshot, batch.fact_snapshot_id)
        resume = session.scalar(select(Resume).where(Resume.batch_id == batch_id, Resume.kind == "job"))
        unit = resume.document_v2["sections"][0]["entries"][0]["units"][0]
        assert unit["text"] == value and unit["claim_refs"] == [fact.claims[0]["claim_id"]]
        assert fact.claims[0]["confirmation"] == "unconfirmed"
        assert fact.source_copies[0]["extracted_text"] == material


def test_provider_failure_reports_category_without_sensitive_message(sessions):
    batch_id = create(sessions)
    class Failure(LocalWriterV2):
        def bundle(self, request):
            raise RuntimeError("fictional-private-probe-secret")
    assert process_batch_v2_once(sessions, lambda _: Failure())
    with v2_write_transaction(sessions, batch_id=batch_id) as session:
        batch = session.get(Batch, batch_id)
        assert batch.status == "failed" and batch.error_code == "GENERATION_FAILED"
        execution = session.scalar(select(TaskExecution).where(TaskExecution.batch_id == batch_id))
        assert execution.status == "failed" and execution.error_code == "GENERATION_FAILED"
        assert "fictional-private-probe-secret" not in repr(execution.input_snapshot)


@pytest.mark.parametrize("stage", ["bundle", "guide"])
def test_denied_provider_access_preserves_safe_retry_reason(sessions, stage):
    import httpx
    batch_id = create(sessions)

    def denied(*args):
        request = httpx.Request("POST", "https://example.invalid")
        raise httpx.HTTPStatusError("private response", request=request,
                                    response=httpx.Response(401, request=request))

    provider = LocalWriterV2()
    setattr(provider, stage, denied)
    assert process_batch_v2_once(sessions, lambda _: provider)
    if stage == "guide":
        assert process_guide_v2_once(sessions, lambda _: provider)
    with v2_write_transaction(sessions, batch_id=batch_id) as session:
        row = session.get(Batch, batch_id) if stage == "bundle" else session.scalar(
            select(Guide).where(Guide.batch_id == batch_id, Guide.status == "failed"))
        assert row.status == "failed" and row.error_code == "MODEL_ACCESS_DENIED"
        failed = session.scalar(select(TaskExecution).where(
            TaskExecution.batch_id == batch_id, TaskExecution.status == "failed"))
        assert failed.error_code == row.error_code


def test_package_flow_allows_long_guides_but_still_blocks_long_resumes(sessions, monkeypatch):
    import json
    from io import BytesIO
    from zipfile import ZipFile
    from docx import Document
    from app import files_v2
    from app.service_v2 import PackageIncomplete, export_v2_package
    batch_id = create(sessions)
    assert process_batch_v2_once(sessions)
    assert process_guide_v2_once(sessions) and process_guide_v2_once(sessions)
    resume_pages = 1

    def render(docx, *, config, locale, paper, page_target):
        is_guide = Document(BytesIO(docx)).paragraphs[0].text == "Interview Guide"
        pages = 3 if is_guide else resume_pages
        return files_v2.PdfResult(b"fixture", pages, page_target, pages > page_target, ())

    monkeypatch.setattr(files_v2, "render_pdf", render)
    monkeypatch.setattr(files_v2, "bundled_converter_config", lambda: None)
    with pytest.raises(PackageIncomplete) as strict:
        export_v2_package(sessions, batch_id=batch_id)
    assert {i["document"] for i in strict.value.overflows} == {"job:guide", "blueprint:guide"}
    with ZipFile(BytesIO(export_v2_package(sessions, batch_id=batch_id, guide_pagination="flow"))) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["guide_pagination"] == "flow"
        assert len(manifest["files"]) == 8
        assert {r["page_count"] for r in manifest["files"] if r["type"] == "guide"} == {3}
    resume_pages = 2
    with pytest.raises(PackageIncomplete) as overflow:
        export_v2_package(sessions, batch_id=batch_id, guide_pagination="flow")
    assert {i["document"] for i in overflow.value.overflows} == {"job:resume", "blueprint:resume"}


def test_mutated_frozen_facts_prevent_late_publish(sessions):
    batch_id = create(sessions)
    class Mutating(LocalWriterV2):
        def bundle(self, request):
            result = super().bundle(request)
            with v2_write_transaction(sessions, batch_id=batch_id) as session:
                batch = session.get(Batch, batch_id)
                session.get(FactSnapshot, batch.fact_snapshot_id).claims = [{"claim_id": "tampered"}]
            return result
    assert process_batch_v2_once(sessions, lambda _: Mutating())
    with v2_write_transaction(sessions, batch_id=batch_id) as session:
        batch = session.get(Batch, batch_id)
        assert batch.status == "running" and batch.plans is None
        assert not session.scalars(select(Resume).where(Resume.batch_id == batch_id)).all()


def test_exhausted_guide_marks_batch_partial(sessions):
    batch_id = create(sessions)
    assert process_batch_v2_once(sessions)
    with v2_write_transaction(sessions, batch_id=batch_id) as session:
        guide = session.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == "blueprint"))
        guide.status = "running"
        guide.attempts = 3
        guide.lease_until = utcnow() - timedelta(seconds=1)
    # The other guide is still processed; exhaustion is recorded first.
    assert process_guide_v2_once(sessions)
    with v2_write_transaction(sessions, batch_id=batch_id) as session:
        batch = session.get(Batch, batch_id)
        guide = session.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == "blueprint"))
        assert guide.status == "failed" and guide.error_code == "ATTEMPTS_EXHAUSTED"
        assert batch.status == "guides_partial"


@pytest.mark.parametrize("corruption", ["coverage_changed", "malformed_coverage", "hash_changed", "malformed_document"])
def test_queued_guide_fails_closed_on_frozen_coverage_mismatch(sessions, corruption):
    batch_id = create(sessions)
    assert process_batch_v2_once(sessions)
    with v2_write_transaction(sessions, batch_id=batch_id) as session:
        guide = session.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == "blueprint"))
        other = session.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == "job"))
        other.status = "ready"  # Isolate the targeted pending branch.
        revision = session.scalar(select(GuideRevision).where(GuideRevision.guide_id == guide.id))
        if corruption == "coverage_changed":
            revision.coverage_unit_ids = []
        elif corruption == "malformed_coverage":
            revision.coverage_unit_ids = {"bad": "not-a-list"}
        elif corruption == "hash_changed":
            guide.guide_input_hash = revision.guide_input_hash = "f" * 64
        else:
            revision.resume_snapshot = {"malformed": True}
        original_binding = (revision.resume_revision, revision.semantic_hash, revision.resume_snapshot)
    calls = []
    def provider_factory(_):
        calls.append(True)
        return LocalWriterV2()
    assert not process_guide_v2_once(sessions, provider_factory)
    assert calls == []
    with v2_write_transaction(sessions, batch_id=batch_id) as session:
        guide = session.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == "blueprint"))
        revision = session.scalar(select(GuideRevision).where(GuideRevision.guide_id == guide.id))
        assert guide.status == "stale" and guide.error_code == "GUIDE_INPUT_STALE"
        assert session.get(Batch, batch_id).status == "guides_partial"
        assert (revision.resume_revision, revision.semantic_hash, revision.resume_snapshot) == original_binding
        assert guide.sections is None and guide.attempts == 0


def test_running_guide_rechecks_coverage_before_publication(sessions):
    batch_id = create(sessions)
    assert process_batch_v2_once(sessions)
    class ChangedCoverage(LocalWriterV2):
        def guide(self, request):
            result = super().guide(request)
            with v2_write_transaction(sessions, batch_id=batch_id) as session:
                guide = session.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == request.kind))
                revision = session.scalar(select(GuideRevision).where(GuideRevision.guide_id == guide.id, GuideRevision.revision == guide.revision))
                revision.coverage_unit_ids = []
            return result
    assert process_guide_v2_once(sessions, lambda _: ChangedCoverage())
    with v2_write_transaction(sessions, batch_id=batch_id) as session:
        guide = session.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == "blueprint"))
        assert guide.status == "stale" and guide.error_code == "GUIDE_INPUT_STALE"
        assert guide.sections is None
        assert session.get(Batch, batch_id).status == "guides_partial"


def test_worker_rejects_generated_coverage_different_from_frozen_input(sessions, monkeypatch):
    from app import worker_v2
    batch_id = create(sessions)
    assert process_batch_v2_once(sessions)
    # Simulate a different rule being applied between claim and response.
    monkeypatch.setattr(worker_v2, "generate_guide_v2", lambda *a, **k: {"coverage_unit_ids": ["unexpected"]})
    assert process_guide_v2_once(sessions)
    with v2_write_transaction(sessions, batch_id=batch_id) as session:
        guide = session.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == "blueprint"))
        assert guide.status == "stale" and guide.error_code == "GUIDE_INPUT_STALE"
        assert guide.sections is None and session.get(Batch, batch_id).status == "guides_partial"
