"""Known initial errors require actual, scoped progress before reassessment."""
from copy import deepcopy

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Guide, GuideRevision, Resume, ResumeRevision
from app.providers import LocalProvider
from app.service import create_batch, get_batch, get_resume_revision, process_batch_once


JD = "虚构岗位：负责装配结构设计与图纸审核。"
MATERIAL = "课程项目中整理装配图纸，记录尺寸检查问题。"
REPAIRED = "围绕课程装配图纸复核关键尺寸，整理尺寸检查清单并记录需要确认的问题。"


@pytest.fixture
def sessions():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()


class PartialRepairProvider(LocalProvider):
    name = "fictional-review-regression"

    def __init__(self, mode):
        self.mode = mode
        self.review_inputs = []
        self.repair_inputs = []

    def bundle(self, jd, material):
        raw = super().bundle(jd, material)
        # A verbatim confirmed job item lets the whitespace case also detect
        # an unnecessary loss of completion when no visible repair happened.
        raw["job"]["items"][0].update(text=material, source_quote=material, achieved=True)
        return raw

    def review(self, payload):
        self.review_inputs.append(deepcopy(payload))
        if len(self.review_inputs) > 1:
            # A fresh model can overlook a previously identified problem;
            # the application must reject incomplete work before this call.
            return {"assessed_scopes": ["job", "blueprint", "plans"], "issues": []}
        kinds = ["job"] if self.mode == "whitespace" else ["job", "job" if self.mode == "shared" else "blueprint"]
        issues = []
        for index, kind in enumerate(kinds, 1):
            item = payload["draft"]["resumes"][kind]["items"][0]
            issues.append({
                "id": f"issue-{index}", "severity": "error", "code": "causal_gap",
                "targets": [{"scope": kind, "target_id": item["id"], "field": "text", "quote": item["text"]}],
                "diagnosis": "该要点没有具体说明本人行动如何形成可检查的产物。",
                "instruction": "仅修复该字段中本人行动和产物之间的因果关系。",
                "jd_quote": "图纸审核",
            })
        return {"assessed_scopes": ["job", "blueprint", "plans"], "issues": issues}

    def repair(self, payload):
        self.repair_inputs.append(deepcopy(payload))
        before = payload["draft"]["resumes"]["job"]["items"][0]["text"]
        return {"edits": [{
            "issue_id": "issue-1", "scope": "job", "target_id": "job-1", "field": "text",
            "before": before, "after": f"  {before}  " if self.mode == "whitespace" else REPAIRED,
        }]}


@pytest.mark.parametrize("mode", ["partial", "whitespace"])
def test_incomplete_or_whitespace_only_repair_cannot_recheck_or_publish(sessions, mode):
    batch = create_batch(sessions, jd=JD, material=MATERIAL, confirmed=True, idempotency_key=f"review-{mode}")
    provider = PartialRepairProvider(mode)
    assert process_batch_once(sessions, provider)
    state = get_batch(sessions, batch.id)
    assert state["status"] == "failed"
    assert state["error_code"] == "CONTENT_REVIEW_FAILED"
    report = state["quality"]["content_review"]
    assert report["status"] == "blocked"
    if mode == "whitespace":
        assert report["reason"] == "no_progress"
    assert len(provider.review_inputs) == 1
    assert len(provider.repair_inputs) == 1
    assert state["resumes"] == {}
    assert state["guides"] == {}
    with sessions() as session:
        for model in (Resume, ResumeRevision, Guide, GuideRevision):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_one_actual_target_edit_can_address_two_errors_sharing_that_target(sessions):
    batch = create_batch(sessions, jd=JD, material=MATERIAL, confirmed=True, idempotency_key="review-shared")
    provider = PartialRepairProvider("shared")
    assert process_batch_once(sessions, provider)
    state = get_batch(sessions, batch.id)
    assert state["status"] == "guides_pending"
    assert state["error_code"] is None
    assert len(provider.review_inputs) == 2
    assert len(provider.repair_inputs) == 1
    report = state["quality"]["content_review"]
    assert report["status"] == "repaired"
    assert report["repair_count"] == 1
    assert len(report["rounds"][0]["issues"]) == 2
    assert state["resumes"]["job"]["items"][0]["text"] == REPAIRED
    assert state["resumes"]["job"]["items"][0]["achieved"] is False
    assert state["resumes"]["blueprint"]["items"] == provider.review_inputs[0]["draft"]["resumes"]["blueprint"]["items"]
    assert len(state["guides"]) == 2


@pytest.mark.parametrize("outcome", ["repaired", "suggestions"])
def test_version_review_status_matches_only_its_relevant_changes_and_issues(sessions, outcome):
    class JobOnlyReview(PartialRepairProvider):
        def review(self, payload):
            result = super().review(payload)
            result["issues"] = result["issues"][:1]
            if outcome == "suggestions":
                result["issues"][0]["severity"] = "suggestion"
            return result

    batch = create_batch(sessions, jd=JD, material=MATERIAL, confirmed=True, idempotency_key=f"version-status-{outcome}")
    provider = JobOnlyReview("shared")
    assert process_batch_once(sessions, provider)
    state = get_batch(sessions, batch.id)
    assert state["quality"]["content_review"]["status"] == outcome
    for kind, expected in (("job", outcome), ("blueprint", "passed")):
        current = state["resumes"][kind]["content_review"]
        historical = get_resume_revision(sessions, batch.id, kind, 1)["content_review"]
        assert current == historical
        assert current["status"] == expected
        assert current["repair_count"] == int(kind == "job" and outcome == "repaired")
        assert len(current["issues"]) == int(kind == "job" and outcome == "suggestions")


def test_shared_plan_repair_counts_for_both_versions_and_history(sessions):
    class SharedPlanReview(PartialRepairProvider):
        def review(self, payload):
            self.review_inputs.append(deepcopy(payload))
            plan = payload["draft"]["plans"][0]
            issues = [] if len(self.review_inputs) > 1 else [{
                "id": "issue-1", "severity": "error", "code": "causal_gap",
                "targets": [{"scope": "plan", "target_id": plan["key"], "field": "task", "quote": plan["task"]}],
                "diagnosis": "项目任务需要说明可以检查的具体产物。",
                "instruction": "仅补充项目任务中的可检查产物。", "jd_quote": "图纸审核",
            }]
            return {"assessed_scopes": ["job", "blueprint", "plans"], "issues": issues}

        def repair(self, payload):
            plan = payload["draft"]["plans"][0]
            return {"edits": [{"issue_id": "issue-1", "scope": "plan", "target_id": plan["key"],
                               "field": "task", "before": plan["task"],
                               "after": "复核课程装配图纸的关键尺寸，并形成可检查的尺寸复核清单。"}]}

    batch = create_batch(sessions, jd=JD, material=MATERIAL, confirmed=True, idempotency_key="shared-plan-status")
    assert process_batch_once(sessions, SharedPlanReview("shared"))
    state = get_batch(sessions, batch.id)
    for kind in ("job", "blueprint"):
        current = state["resumes"][kind]["content_review"]
        assert current["status"] == "repaired"
        assert current["repair_count"] == 1
        assert get_resume_revision(sessions, batch.id, kind, 1)["content_review"] == current
