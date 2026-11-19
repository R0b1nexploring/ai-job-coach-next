"""Content-review integration contracts using only fictional, temporary data."""
from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

import pytest
from sqlalchemy import create_engine, delete, event, func, select, update
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import Batch, Guide, GuideRevision, Resume, ResumeRevision, TaskExecution, utcnow
from app.providers import LocalProvider
from app.service import (
    LEASE_SECONDS, get_batch, get_guide_revision, get_resume_revision, list_executions,
    process_batch_once, process_guide_once, save_resume, create_batch,
)


JD = "虚构机械岗位：负责结构设计、装配图输出与方案验证。"
MATERIAL = "虚构课程项目：绘制装配图并整理零件清单，进行方案评审。"
REPAIRED_TEXT = "围绕装配图说明本人负责的尺寸检查步骤，并将零件清单作为可核对产物。"
REPAIRED_TASK = "以现有课程图纸为输入，明确本人承担的尺寸复核任务及可检查的输出。"
SCOPES = ["job", "blueprint", "plans"]


@pytest.fixture
def sessions(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'content-review.sqlite'}",
        connect_args={"check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, expire_on_commit=False)
    finally:
        engine.dispose()


def new_batch(sessions, *, confirmed=False):
    return create_batch(
        sessions, jd=JD, material=MATERIAL, confirmed=confirmed,
        idempotency_key="fictional-content-review",
    )


def target_value(draft, scope, target_id, field):
    if scope == "plan":
        return next(plan for plan in draft["plans"] if plan["key"] == target_id)[field]
    resume = draft["resumes"][scope]
    if not target_id:
        return resume[field]
    return next(item for item in resume["items"] if item["id"] == target_id)[field]


class RepairingProvider(LocalProvider):
    name = "fictional-review"

    def __init__(self, *, scope="job", target_id="job-1", field="text", after=REPAIRED_TEXT):
        self.scope, self.target_id, self.field, self.after = scope, target_id, field, after
        self.review_inputs = []
        self.repair_inputs = []

    def issue(self, payload, *, severity="error"):
        before = target_value(payload["draft"], self.scope, self.target_id, self.field)
        return {
            "id": "issue-1", "severity": severity, "code": "causal_gap",
            "targets": [{
                "scope": self.scope, "target_id": self.target_id,
                "field": self.field, "quote": before,
            }],
            "diagnosis": "此处未具体说明本人行动与可检查产物之间的关系。",
            "instruction": "仅改写所指出的字段，说明本人任务及可检查的产物，不增加既成经历。",
            "jd_quote": "结构设计",
        }

    def review(self, payload):
        self.review_inputs.append(deepcopy(payload))
        return {"assessed_scopes": SCOPES, "issues": [self.issue(payload)] if len(self.review_inputs) == 1 else []}

    def repair(self, payload):
        self.repair_inputs.append(deepcopy(payload))
        return {"edits": [{
            "issue_id": "issue-1", "scope": self.scope, "target_id": self.target_id,
            "field": self.field,
            "before": deepcopy(target_value(payload["draft"], self.scope, self.target_id, self.field)),
            "after": deepcopy(self.after),
        }]}


def finish_guides(sessions, batch, provider):
    assert process_guide_once(sessions, provider)
    assert process_guide_once(sessions, provider)
    return get_batch(sessions, batch.id)


def save_changes(sessions, batch, **changes):
    current = get_batch(sessions, batch.id)["resumes"]["job"]
    fields = {key: deepcopy(current[key]) for key in ("revision", "title", "summary", "items")}
    fields.update(changes)
    return save_resume(sessions, batch.id, "job", **fields)


def assert_no_delivery(sessions, batch):
    state = get_batch(sessions, batch.id)
    assert state["status"] == "failed"
    assert state["error_code"] == "CONTENT_REVIEW_FAILED"
    assert state["resumes"] == {}
    assert state["guides"] == {}
    assert isinstance(state["quality"]["content_review"], dict)
    assert state["quality"]["content_review"]
    with sessions() as session:
        for model in (Resume, ResumeRevision, Guide, GuideRevision):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_only_final_repaired_copy_reaches_resumes_revisions_and_guides(sessions):
    class TwoItems(RepairingProvider):
        def bundle(self, jd, material):
            raw = super().bundle(jd, material)
            raw["job"]["items"].append({
                "plan_key": "project-1", "text": "整理图纸版本与课程汇报材料，保留便于复核的项目记录。",
                "source_quote": "", "achieved": False,
            })
            return raw

    provider = TwoItems()
    batch = new_batch(sessions)
    assert process_batch_once(sessions, provider)
    state = finish_guides(sessions, batch, provider)
    initial = provider.review_inputs[0]["draft"]
    assert state["status"] == "ready"
    assert len(provider.review_inputs) == 2
    assert len(provider.repair_inputs) == 1
    assert all(issue["severity"] == "error" for issue in provider.repair_inputs[0]["issues"])
    job = state["resumes"]["job"]
    assert job["items"][0]["text"] == REPAIRED_TEXT
    assert job["items"][1] == initial["resumes"]["job"]["items"][1]
    assert job["title"] == initial["resumes"]["job"]["title"]
    assert job["summary"] == initial["resumes"]["job"]["summary"]
    assert state["resumes"]["blueprint"]["items"] == initial["resumes"]["blueprint"]["items"]
    for field in ("id", "plan_key", "source_quote", "source_status", "verification_status", "achieved"):
        assert job["items"][0][field] == initial["resumes"]["job"]["items"][0][field]
    assert job["content_review"]["status"] == "repaired"
    for kind in ("job", "blueprint"):
        resume = state["resumes"][kind]
        revision = get_resume_revision(sessions, batch.id, kind, 1)
        guide_revision = get_guide_revision(sessions, batch.id, kind, 1)
        assert revision["items"] == resume["items"]
        assert guide_revision["resume"]["items"] == resume["items"]
        assert guide_revision["plans"] == resume["plans"]
        assert [entry["item_id"] for entry in state["guides"][kind]["entries"]] == [item["id"] for item in resume["items"]]
    assert REPAIRED_TEXT in state["guides"]["job"]["entries"][0]["answer_outline"]


def test_shared_plan_repair_is_frozen_into_both_guide_inputs(sessions):
    provider = RepairingProvider(scope="plan", target_id="project-1", field="task", after=REPAIRED_TASK)
    batch = new_batch(sessions)
    assert process_batch_once(sessions, provider)
    state = finish_guides(sessions, batch, provider)
    initial = provider.review_inputs[0]["draft"]
    for kind in ("job", "blueprint"):
        resume = state["resumes"][kind]
        assert resume["items"] == initial["resumes"][kind]["items"]
        assert resume["plans"][0]["key"] == "project-1"
        assert resume["plans"][0]["task"] == REPAIRED_TASK
        assert get_guide_revision(sessions, batch.id, kind, 1)["plans"] == resume["plans"]
        assert REPAIRED_TASK in state["guides"][kind]["entries"][0]["answer_outline"]


def test_repaired_confirmed_verbatim_item_is_no_longer_completed(sessions):
    class PreviouslyVerbatim(RepairingProvider):
        def bundle(self, jd, material):
            raw = super().bundle(jd, material)
            raw["job"]["items"][0].update(text=material, source_quote=material, achieved=True)
            return raw

    provider = PreviouslyVerbatim()
    batch = new_batch(sessions, confirmed=True)
    assert process_batch_once(sessions, provider)
    assert provider.review_inputs[0]["draft"]["resumes"]["job"]["items"][0]["achieved"] is True
    item = get_batch(sessions, batch.id)["resumes"]["job"]["items"][0]
    assert item["text"] == REPAIRED_TEXT
    assert item["source_quote"] == MATERIAL
    assert item["source_status"] == "user_confirmed"
    assert item["verification_status"] == "ai_generated"
    assert item["achieved"] is False


@pytest.mark.parametrize("outcome", ["passed", "suggestions", "none", "exception", "invalid"])
def test_first_review_nonblocking_outcomes_do_not_rewrite_draft(sessions, outcome):
    class ReviewOnly(RepairingProvider):
        def review(self, payload):
            self.review_inputs.append(deepcopy(payload))
            if outcome == "none":
                return None
            if outcome == "exception":
                raise RuntimeError("fictional unavailable reviewer")
            if outcome == "invalid":
                return {"assessed_scopes": ["job"], "issues": []}
            return {"assessed_scopes": SCOPES, "issues": [self.issue(payload, severity="suggestion")] if outcome == "suggestions" else []}

        def repair(self, payload):
            raise AssertionError("Nonblocking reviews must not request a repair")

    provider = ReviewOnly()
    batch = new_batch(sessions)
    assert process_batch_once(sessions, provider)
    state = get_batch(sessions, batch.id)
    assert state["status"] == "guides_pending"
    expected = outcome if outcome in {"passed", "suggestions"} else "unavailable"
    assert state["quality"]["content_review"]["status"] == expected
    for kind in ("job", "blueprint"):
        assert state["resumes"][kind]["items"] == provider.review_inputs[0]["draft"]["resumes"][kind]["items"]
    assert state["resumes"]["job"]["content_review"]["status"] == expected


@pytest.mark.parametrize("failure", ["repair_exception", "no_change", "review_none", "review_exception", "still_error"])
def test_unsuccessful_repair_retains_report_without_half_a_delivery(sessions, failure):
    class Unsuccessful(RepairingProvider):
        def review(self, payload):
            self.review_inputs.append(deepcopy(payload))
            if len(self.review_inputs) > 1:
                if failure == "review_none":
                    return None
                if failure == "review_exception":
                    raise RuntimeError("fictional second review unavailable")
            return {"assessed_scopes": SCOPES, "issues": [self.issue(payload)]}

        def repair(self, payload):
            if failure == "repair_exception":
                raise RuntimeError("fictional repair failure")
            result = super().repair(payload)
            if failure == "no_change":
                result["edits"][0]["after"] = result["edits"][0]["before"]
            return result

    provider = Unsuccessful()
    batch = new_batch(sessions)
    assert process_batch_once(sessions, provider)
    assert_no_delivery(sessions, batch)
    assert len(provider.review_inputs) <= 2
    assert len(provider.repair_inputs) <= 1


@pytest.mark.parametrize("extra_field", ["achieved", "source_status", "plan_key"])
def test_repair_cannot_promote_provenance_or_change_project_membership(sessions, extra_field):
    class OutOfScope(RepairingProvider):
        def repair(self, payload):
            result = super().repair(payload)
            item = payload["draft"]["resumes"]["job"]["items"][0]
            result["edits"].append({
                "issue_id": "issue-1", "scope": "job", "target_id": "job-1",
                "field": extra_field, "before": item[extra_field],
                "after": {"achieved": True, "source_status": "user_confirmed", "plan_key": "invented-project"}[extra_field],
            })
            return result

    batch = new_batch(sessions)
    assert process_batch_once(sessions, OutOfScope())
    assert_no_delivery(sessions, batch)


@pytest.mark.parametrize("scope,target_id,field", [("blueprint", "blueprint-1", "text"), ("job", "", "summary")])
def test_repair_cannot_rewrite_unreviewed_copy(sessions, scope, target_id, field):
    class ExtraRewrite(RepairingProvider):
        def repair(self, payload):
            result = super().repair(payload)
            result["edits"].append({
                "issue_id": "issue-1", "scope": scope, "target_id": target_id, "field": field,
                "before": target_value(payload["draft"], scope, target_id, field),
                "after": "这一项没有被评审标记，不应被模型自动改写成其他内容。",
            })
            return result

    batch = new_batch(sessions)
    assert process_batch_once(sessions, ExtraRewrite())
    assert_no_delivery(sessions, batch)


def test_personal_header_is_excluded_from_review_and_repair_payloads(sessions):
    batch = create_batch(
        sessions, jd=JD, material=MATERIAL, confirmed=False,
        idempotency_key="fictional-review-header",
        profile={"name": "虚构同学", "email": "private-fictional@example.invalid"},
    )
    provider = RepairingProvider()
    assert process_batch_once(sessions, provider)
    assert len(provider.review_inputs) == 2
    assert len(provider.repair_inputs) == 1
    for payload in [*provider.review_inputs, *provider.repair_inputs]:
        assert "profile" not in payload
        assert "private-fictional@example.invalid" not in str(payload)
        for resume in payload["draft"]["resumes"].values():
            assert "header" not in resume
    assert get_batch(sessions, batch.id)["resumes"]["job"]["header"]["email"] == "private-fictional@example.invalid"


def test_user_semantic_edit_stales_current_review_but_preserves_historical_report(sessions):
    batch = new_batch(sessions)
    provider = RepairingProvider()
    assert process_batch_once(sessions, provider)
    before = finish_guides(sessions, batch, provider)
    old_revision = get_resume_revision(sessions, batch.id, "job", 1)
    old_guide = get_guide_revision(sessions, batch.id, "job", 1)
    items = deepcopy(before["resumes"]["job"]["items"])
    items[0]["text"] = "虚构人工改稿：整理课程设计的零件清单，并标注尺寸检查的待核对事项。"
    after = save_changes(sessions, batch, items=items)
    assert after["resumes"]["job"]["content_review"]["status"] == "stale"
    assert after["guides"]["job"]["status"] == "stale"
    assert after["resumes"]["blueprint"] == before["resumes"]["blueprint"]
    assert after["guides"]["blueprint"] == before["guides"]["blueprint"]
    assert get_resume_revision(sessions, batch.id, "job", 1) == old_revision
    assert get_guide_revision(sessions, batch.id, "job", 1) == old_guide
    assert old_revision["content_review"]["status"] == "repaired"


@pytest.mark.parametrize("change", ["header", "lock"])
def test_display_or_lock_change_does_not_stale_content_review(sessions, change):
    batch = new_batch(sessions)
    provider = RepairingProvider()
    assert process_batch_once(sessions, provider)
    before = finish_guides(sessions, batch, provider)
    if change == "header":
        after = save_changes(sessions, batch, header={"name": "虚构同学", "email": "fictional@example.invalid"})
    else:
        items = deepcopy(before["resumes"]["job"]["items"])
        items[0]["locked"] = True
        after = save_changes(sessions, batch, items=items)
    assert after["resumes"]["job"]["revision"] == 2
    assert after["resumes"]["job"]["semantic_hash"] == before["resumes"]["job"]["semantic_hash"]
    assert after["resumes"]["job"]["content_review"]["status"] == "repaired"
    assert after["guides"]["job"]["status"] == "ready"


@pytest.mark.parametrize("stage", ["review", "repair"])
@pytest.mark.parametrize("event_name", ["expire", "delete"])
def test_review_and_repair_cannot_publish_after_losing_batch(sessions, stage, event_name):
    batch = new_batch(sessions)

    def lose_batch():
        with sessions() as session:
            if event_name == "delete":
                session.execute(delete(Batch).where(Batch.id == batch.id))
            else:
                session.execute(update(Batch).where(Batch.id == batch.id).values(lease_until=utcnow() - timedelta(seconds=1)))
            session.commit()

    class LosesOwnership(RepairingProvider):
        def review(self, payload):
            result = super().review(payload)
            if stage == "review":
                lose_batch()
            return result

        def repair(self, payload):
            result = super().repair(payload)
            if stage == "repair":
                lose_batch()
            return result

    provider = LosesOwnership()
    assert process_batch_once(sessions, provider)
    with sessions() as session:
        for model in (Resume, ResumeRevision, Guide, GuideRevision):
            assert session.scalar(select(func.count()).select_from(model)) == 0
        if event_name == "delete":
            assert session.get(Batch, batch.id) is None
            assert session.scalar(select(func.count()).select_from(TaskExecution)) == 0
    if event_name == "expire":
        state = get_batch(sessions, batch.id)
        assert state["status"] == "running"
        assert state["error_code"] is None
        assert list_executions(sessions, batch.id)[0]["status"] == "lease_expired"
    if stage == "review":
        assert provider.repair_inputs == []
    else:
        assert len(provider.review_inputs) == 1


def test_slow_review_pipeline_renews_each_stage_and_reports_progress(sessions, monkeypatch):
    # Each external phase is shorter than one lease, but their combined time
    # exceeds the original lease. A fixed deadline would lose the valid result.
    clock = [utcnow()]
    started_at = clock[0]
    phase_duration = timedelta(seconds=400)
    assert phase_duration < timedelta(seconds=LEASE_SECONDS)
    monkeypatch.setattr("app.service.utcnow", lambda: clock[0])
    batch = new_batch(sessions)
    phases = []
    progress_at = []

    def finish_slow_phase(name):
        with sessions() as session:
            deadline = session.get(Batch, batch.id).lease_until
        # SQLite reads UTC DateTime values without their timezone metadata.
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=clock[0].tzinfo)
        clock[0] += phase_duration
        assert clock[0] < deadline
        phases.append((name, clock[0]))

    class SlowProvider(RepairingProvider):
        def bundle(self, jd, material):
            result = super().bundle(jd, material)
            finish_slow_phase("bundle")
            return result

        def review(self, payload):
            result = super().review(payload)
            finish_slow_phase("review")
            return result

        def repair(self, payload):
            result = super().repair(payload)
            finish_slow_phase("repair")
            return result

    assert process_batch_once(sessions, SlowProvider(), progress=lambda: progress_at.append(clock[0]))
    state = get_batch(sessions, batch.id)
    assert [name for name, _ in phases] == ["bundle", "review", "repair", "review"]
    assert clock[0] - started_at > timedelta(seconds=LEASE_SECONDS)
    assert all(finished_at in progress_at for _, finished_at in phases)
    assert state["status"] == "guides_pending"
    assert state["quality"]["content_review"]["status"] == "repaired"
    assert set(state["resumes"]) == {"job", "blueprint"}
    assert set(state["guides"]) == {"job", "blueprint"}
    executions = list_executions(sessions, batch.id)
    assert len(executions) == 1
    assert executions[0]["status"] == "succeeded"
