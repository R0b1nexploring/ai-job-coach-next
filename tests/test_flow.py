from __future__ import annotations

from datetime import timedelta
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Batch, Guide, utcnow
from app.providers import FallbackProvider, LocalProvider
from app.service import InputConflict, create_batch, get_batch, process_batch_once, process_guide_once, retry_guide

JD = "机械结构设计工程师\n负责装配结构设计、图纸表达和可制造性评审。\n要求说明设计约束和验证方法。"
MATERIAL = "毕业项目：绘制装配图并整理零部件清单，完成课程设计汇报。"


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'flow.sqlite'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine, expire_on_commit=False)
    engine.dispose()


def test_dual_resume_guides_and_provenance(sessions):
    batch = create_batch(sessions, jd=JD, material=MATERIAL, confirmed=True, idempotency_key="first-request")
    provider = LocalProvider()
    assert process_batch_once(sessions, provider)
    after_resumes = get_batch(sessions, batch.id)
    assert after_resumes["status"] == "guides_pending"
    assert set(after_resumes["resumes"]) == {"job", "blueprint"}
    assert after_resumes["resumes"]["job"]["items"][0]["source_status"] == "user_confirmed"
    assert after_resumes["resumes"]["job"]["items"][0]["verification_status"] == "ai_generated"
    # 素材确认只覆盖引用原文，不能替模型扩写的整条正文确认完成。
    assert after_resumes["resumes"]["job"]["items"][0]["achieved"] is False
    assert after_resumes["resumes"]["job"]["items"][0]["flags"] == ["achievement_unverified"]
    assert after_resumes["resumes"]["blueprint"]["items"][0]["verification_status"] == "ai_generated"
    assert after_resumes["resumes"]["blueprint"]["items"][0]["achieved"] is False
    assert process_guide_once(sessions, provider)
    assert process_guide_once(sessions, provider)
    result = get_batch(sessions, batch.id)
    assert result["status"] == "ready"
    for kind in ("job", "blueprint"):
        assert result["guides"][kind]["status"] == "ready"
        assert result["guides"][kind]["entries"][0]["item_id"] == result["resumes"][kind]["items"][0]["id"]
    assert result["resumes"]["job"]["items"][0]["text"] != result["resumes"]["blueprint"]["items"][0]["text"]


@pytest.mark.parametrize("kind", ["job", "blueprint"])
def test_generated_item_can_be_completed_by_explicit_user_confirmation(sessions, kind):
    from app.service import get_resume_revision, save_resume

    batch = create_batch(sessions, jd=JD, material=MATERIAL, confirmed=True, idempotency_key=f"confirm-{kind}")
    assert process_batch_once(sessions, LocalProvider())
    before = get_batch(sessions, batch.id)["resumes"][kind]
    assert before["items"][0]["achieved"] is False
    assert before["items"][0]["verification_status"] == "ai_generated"
    items = [dict(item, confirm=True, achieved=True) for item in before["items"]]
    after = save_resume(
        sessions, batch.id, kind, revision=before["revision"],
        title=before["title"], summary=before["summary"], items=items,
    )["resumes"][kind]
    assert after["items"][0]["text"] == before["items"][0]["text"]
    assert after["items"][0]["achieved"] is True
    assert after["items"][0]["verification_status"] == "user_confirmed"
    assert get_resume_revision(sessions, batch.id, kind, 1)["items"][0]["achieved"] is False


def test_duplicate_request_reuses_batch_and_changed_input_conflicts(sessions):
    first = create_batch(sessions, jd=JD, material="", confirmed=False, idempotency_key="same-request")
    second = create_batch(sessions, jd=JD, material="", confirmed=False, idempotency_key="same-request")
    assert first.id == second.id
    with pytest.raises(InputConflict):
        create_batch(sessions, jd=JD, material=MATERIAL, confirmed=False, idempotency_key="same-request")
    with sessions() as session:
        assert len(session.scalars(select(Batch)).all()) == 1


def test_guide_failure_is_independent_and_retryable(sessions):
    class FailBlueprint(LocalProvider):
        def guide(self, kind, resume, plans, jd):
            if kind == "blueprint":
                raise RuntimeError("synthetic model failure")
            return super().guide(kind, resume, plans, jd)

    batch = create_batch(sessions, jd=JD, material=MATERIAL, confirmed=False, idempotency_key="failure-case")
    assert process_batch_once(sessions, LocalProvider())
    assert process_guide_once(sessions, FailBlueprint())
    assert process_guide_once(sessions, FailBlueprint())
    partial = get_batch(sessions, batch.id)
    assert partial["status"] == "guides_partial"
    assert partial["guides"]["job"]["status"] == "ready"
    assert partial["guides"]["blueprint"]["status"] == "failed"
    retry_guide(sessions, batch.id, "blueprint")
    assert process_guide_once(sessions, LocalProvider())
    assert get_batch(sessions, batch.id)["status"] == "ready"


def test_expired_claim_recovers_and_semantic_failure_falls_back(sessions, caplog):
    class WrongReference(LocalProvider):
        name = "wrong-reference"
        def bundle(self, jd, material):
            raw = super().bundle(jd, material)
            raw["job"]["items"][0]["plan_key"] = "missing-plan"
            return raw

    batch = create_batch(sessions, jd=JD, material="", confirmed=False, idempotency_key="recovery-case")
    with sessions() as session:
        saved = session.get(Batch, batch.id)
        saved.status = "running"
        saved.attempts = 1
        saved.lease_until = utcnow() - timedelta(minutes=1)
        session.commit()
    assert process_batch_once(sessions, FallbackProvider(WrongReference()))
    result = get_batch(sessions, batch.id)
    assert result["status"] == "guides_pending"
    assert result["source"] == "degraded_local"
    assert "provider fallback operation=bundle category=normalized_output_rejected http_status=-" in caplog.text
    with sessions() as session:
        assert session.get(Batch, batch.id).attempts == 2


def test_guide_normalization_fallback_is_logged_without_payload(sessions, caplog):
    class WrongGuideReference(LocalProvider):
        name = "fictional-compatible-guide"
        def guide(self, kind, resume, plans, jd):
            raw = super().guide(kind, resume, plans, jd)
            raw["entries"][0]["item_index"] = 42
            return raw

    batch = create_batch(sessions, jd=JD, material="", confirmed=False, idempotency_key="guide-normalization-log")
    assert process_batch_once(sessions, LocalProvider())
    assert process_guide_once(sessions, FallbackProvider(WrongGuideReference()))
    result = get_batch(sessions, batch.id)
    assert result["guides"]["job"]["status"] == "ready"
    assert result["guides"]["job"]["source"] == "degraded_local"
    assert "provider fallback operation=guide:job category=normalized_output_rejected http_status=-" in caplog.text
    filtered = subprocess.run(["awk", "-f", str(Path(__file__).parents[1] / "scripts" / "provider-diagnostic-filter.awk")], input=caplog.text, text=True, capture_output=True, check=True).stdout
    assert "provider_error operation=guide:job category=normalized_output_rejected http_status=- count=1" in filtered
    assert "42" not in filtered


def test_guide_step_arrays_normalize_to_display_text():
    from app.content import normalize_guide
    resume = {"items": [{"id": "job-1", "achieved": False}]}
    raw = {"entries": [{
        "item_index": 0,
        "question": "如何说明项目？",
        "answer_outline": ["说明背景", "说明个人行动"],
        "contribution_boundary": ["区分团队工作", "说明本人任务"],
        "verification": ["展示图纸", "核对记录"],
        "practice_step": ["整理资料", "练习追问"],
    }]}
    result = normalize_guide(raw, resume, "job")
    assert result[0]["answer_outline"] == "说明背景；说明个人行动"
    assert result[0]["item_id"] == "job-1"


def test_unfinished_blueprint_guide_cannot_ask_for_past_experience():
    from app.content import normalize_guide
    resume = {"items": [{"id": "blueprint-1", "achieved": False}]}
    raw = {"entries": [{"item_index": 0, "question": "请介绍一次你如何完成供应商询价？", "answer_outline": "说明计划和实施条件。", "contribution_boundary": "不得声称已完成。", "verification": "保存报价记录。", "practice_step": "准备询价资料。"}]}
    with pytest.raises(ValueError):
        normalize_guide(raw, resume, "blueprint")


def test_paraphrased_source_cannot_become_confirmed():
    from app.content import normalize_bundle
    raw = LocalProvider().bundle(JD, MATERIAL)
    raw["job"]["items"][0]["source_quote"] = "用户做了许多机械工作"
    result = normalize_bundle(raw, JD, MATERIAL, True)
    item = result["resumes"]["job"]["items"][0]
    assert item["source_quote"] == ""
    assert item["source_status"] == "none"
    assert item["verification_status"] == "ai_generated"
    assert item["achieved"] is False


def test_unmatched_target_is_explicitly_inferred_not_jd_evidence():
    from app.content import normalize_bundle
    raw = LocalProvider().bundle(JD, "")
    raw["plans"][0]["target_evidence"] = "要求熟练掌握不存在的专业仿真工具"
    result = normalize_bundle(raw, JD, "", False)
    plan = result["plans"][0]
    assert plan["target_evidence_status"] == "ai_inferred"
    assert plan["target_evidence"] == ""
    assert plan["target_inference"] == "要求熟练掌握不存在的专业仿真工具"


def test_exhausted_expired_work_is_not_left_running(sessions):
    batch = create_batch(sessions, jd=JD, material="", confirmed=False, idempotency_key="exhausted-case")
    with sessions() as session:
        saved = session.get(Batch, batch.id)
        saved.status = "running"
        saved.attempts = 3
        saved.lease_until = utcnow() - timedelta(minutes=1)
        session.commit()
    assert process_batch_once(sessions, LocalProvider()) is False
    result = get_batch(sessions, batch.id)
    assert result["status"] == "failed"
    assert result["error_code"] == "ATTEMPTS_EXHAUSTED"


def test_edit_stales_only_matching_guide_and_exports_saved_version(sessions):
    from io import BytesIO
    from docx import Document
    from pypdf import PdfReader
    from app.files import render_docx, render_pdf
    from app.service import list_revisions, save_resume

    batch = create_batch(sessions, jd=JD, material=MATERIAL, confirmed=True, idempotency_key="editable-case")
    provider = LocalProvider()
    assert process_batch_once(sessions, provider)
    assert process_guide_once(sessions, provider)
    assert process_guide_once(sessions, provider)
    before = get_batch(sessions, batch.id)
    job = before["resumes"]["job"]
    changed = [dict(item) for item in job["items"]]
    changed[0]["text"] = "本人整理装配图和零部件清单，准备设计评审材料。"
    after = save_resume(sessions, batch.id, "job", revision=1, title=job["title"], summary=job["summary"], items=changed)
    assert after["resumes"]["job"]["revision"] == 2
    assert after["guides"]["job"]["status"] == "stale"
    assert after["guides"]["blueprint"]["status"] == "ready"
    assert len(list_revisions(sessions, batch.id, "job")) == 2
    with pytest.raises(InputConflict):
        save_resume(sessions, batch.id, "job", revision=1, title=job["title"], summary=job["summary"], items=changed)
    doc = Document(BytesIO(render_docx(after["resumes"]["job"])))
    assert changed[0]["text"] in "\n".join(paragraph.text for paragraph in doc.paragraphs)
    pdf = PdfReader(BytesIO(render_pdf(after["resumes"]["job"])))
    assert pdf.pages and abs(float(pdf.pages[0].mediabox.width) - 595.28) < 1
    retry_guide(sessions, batch.id, "job")
    assert process_guide_once(sessions, provider)
    assert get_batch(sessions, batch.id)["status"] == "ready"
