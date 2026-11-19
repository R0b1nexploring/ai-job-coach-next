from copy import deepcopy
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.content_v2 import text_hash
from app.db import Base, make_engine
from app.generation_v2 import LocalWriterV2
from app.models import Guide, GuideRevision, Resume, ResumeRevision
from app.service_v2 import create_v2_batch, get_v2_batch, save_v2_resume
from app.worker_v2 import process_batch_v2_once, process_guide_v2_once

JD = "虚构岗位负责工程资料整理、流程检查与文档维护工作。"


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("COACH_PROVIDER", "local")
    engine = make_engine(f"sqlite:///{tmp_path / 'guide-frozen.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    yield sessions
    engine.dispose()


def create_with_two_plans(sessions):
    source = "私密姓名探针：林测试；私密邮箱：fiction@example.invalid；已整理设备记录"
    quote = "整理设备记录"
    start = source.index(quote)
    batch = create_v2_batch(sessions, jd=JD, material=source, candidates=[{
        "fact_type": "action", "value": quote, "start": start, "end": start + len(quote),
        "quote_hash": text_hash(quote), "confirmed": True, "completion": "completed",
    }], header={"name": "林测试", "email": "fiction@example.invalid"}, resume_locale="zh-CN",
        guide_locale="en", idempotency_key="fictional-guide-freeze")

    class TwoPlans(LocalWriterV2):
        def bundle(self, request):
            result = super().bundle(request)
            plan = deepcopy(result["plans"][0]); plan["temp_key"] = "job-plan"
            plan["title"] = "求职版独有方案"; plan["background"] = "求职版独有背景"
            result["plans"].append(plan)
            source_section = next(s for s in result["blueprint"]["sections"] if s["type"] == "projects")
            job_section = deepcopy(source_section); job_section["temp_key"] = "job-proj"
            job_section["entries"][0]["temp_key"] = "job-entry"
            job_section["entries"][0]["plan_ref"] = "job-plan"
            unit = job_section["entries"][0]["units"][0];unit["temp_key"] = "job-proposal"
            unit["plan_ref"] = "job-plan";unit["claim_refs"] = [request.facts[0].claim_id]
            result["job"]["sections"].append(job_section)
            return result
    assert process_batch_v2_once(sessions, lambda _: TwoPlans())
    return batch["id"], source


def test_guide_provider_gets_only_version_bound_plan_and_projected_facts(setup):
    sessions = setup
    batch_id, source = create_with_two_plans(sessions)
    received = []
    class Capture(LocalWriterV2):
        def guide(self, request):
            received.append(request.model_dump(mode="json"))
            return super().guide(request)
    assert process_guide_v2_once(sessions, lambda _: Capture())
    assert process_guide_v2_once(sessions, lambda _: Capture())
    by_kind = {item["kind"]: item for item in received}
    assert set(by_kind) == {"job", "blueprint"}
    job, blue = by_kind["job"], by_kind["blueprint"]
    assert job["plans"][0]["title"] == "求职版独有方案"
    assert all(plan["title"] != "求职版独有方案" for plan in blue["plans"])
    assert job["guide_locale"] == blue["guide_locale"] == "en"
    assert job["facts"] and job["facts"][0]["confirmation"] == "user_confirmed"
    assert job["facts"][0]["completion"] == "completed"
    assert job["facts"][0]["source_types"] == ["manual"] and job["facts"][0]["has_evidence"]
    assert any(unit["claim_refs"] and unit["plan_ref"] for unit in job["units"])
    assert all(unit["plan_ref"] in {plan["key"] for plan in job["plans"]} for unit in job["units"] if unit["plan_ref"])
    data = json.dumps(received, ensure_ascii=False)
    assert source not in data and "林测试" not in data and "fiction@example.invalid" not in data
    assert "source_copies" not in data and "extracted_text" not in data and "header" not in data
    with sessions() as db:
        from app.models import FactSnapshot
        fact_id = db.scalar(select(FactSnapshot)).claims[0]["claim_id"]
        plan_id = db.scalar(select(Resume).where(Resume.batch_id == batch_id, Resume.kind == "job")).plans[-1]["key"]
    assert fact_id not in data and plan_id not in data


def test_queued_guide_rejects_late_completion_after_edit(setup):
    sessions = setup
    batch_id, _ = create_with_two_plans(sessions)
    captures = []
    class ConcurrentEdit(LocalWriterV2):
        def guide(self, request):
            captures.append(request.model_dump(mode="json"))
            batch = get_v2_batch(sessions, batch_id)
            version = batch["resumes"][request.kind]
            save_v2_resume(sessions, batch_id=batch_id, kind=request.kind, expected_revision=version["revision"],
                           title=version["document"]["title"], header=version["header"],
                           unit_texts={version["document"]["summary"]["unit_id"]: "已编辑的虚构摘要"})
            return super().guide(request)
    assert process_guide_v2_once(sessions, lambda _: ConcurrentEdit())
    assert len(captures) == 1
    with sessions() as db:
        kind = captures[0]["kind"]
        guide = db.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == kind))
        assert guide.status == "stale" and guide.sections is None
        old = db.scalar(select(GuideRevision).where(GuideRevision.guide_id == guide.id, GuideRevision.revision == 1))
        assert old.status == "superseded" and old.sections is None
        resume = db.scalar(select(Resume).where(Resume.batch_id == batch_id, Resume.kind == kind))
        assert resume.revision == 2
        historical = db.scalar(select(ResumeRevision).where(ResumeRevision.resume_id == resume.id, ResumeRevision.revision == 1))
        assert historical.document_v2["summary"]["text"] != "已编辑的虚构摘要"


def test_guide_refuses_publish_if_its_frozen_plan_changes_during_call(setup):
    sessions = setup
    batch_id, _ = create_with_two_plans(sessions)
    called = []
    class MutateFrozenPlan(LocalWriterV2):
        def guide(self, request):
            called.append(request.kind)
            with sessions() as db:
                guide = db.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == request.kind))
                rev = db.scalar(select(GuideRevision).where(GuideRevision.guide_id == guide.id, GuideRevision.revision == guide.revision))
                plans = deepcopy(rev.plans)
                plans[0]["background"] = "运行中被篡改的背景"
                rev.plans = plans
                db.commit()
            return super().guide(request)
    assert process_guide_v2_once(sessions, lambda _: MutateFrozenPlan())
    with sessions() as db:
        from app.models import TaskExecution
        guide = db.scalar(select(Guide).where(Guide.batch_id == batch_id, Guide.kind == called[0]))
        execution = db.scalar(select(TaskExecution).where(TaskExecution.guide_id == guide.id))
        assert guide.status == "running" and guide.sections is None
        assert execution.status == "superseded"
