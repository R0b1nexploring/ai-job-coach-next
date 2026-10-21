from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
import os
from threading import Barrier
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, delete, event, func, select, text, update
from sqlalchemy.orm import Session, sessionmaker

from app.db import Base
from app.models import Batch, Guide, GuideRevision, Resume, ResumeRevision, TaskExecution, utcnow
from app.providers import LocalProvider
from app.service import (
    InputConflict, _claim_batch, _claim_guide, create_batch, get_batch,
    get_guide_revision, get_resume_revision, list_executions, list_guide_revisions,
    list_revisions, process_batch_once, process_guide_once, renew_execution,
    retry_batch, retry_guide, save_resume,
)

JD = "虚构机械岗位：负责结构设计、装配图输出与方案验证。"
MATERIAL = "虚构课程项目：绘制装配图并整理零件清单，进行方案评审。"


@pytest.fixture
def sessions(tmp_path):
    url = os.getenv("COACH_PG_TEST_URL")
    if url:
        engine = create_engine(url, pool_pre_ping=True)
        schema = "reliability_" + uuid4().hex
        with engine.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped = engine.execution_options(schema_translate_map={None: schema})
        Base.metadata.create_all(scoped)
    else:
        engine = create_engine(f"sqlite:///{tmp_path / 'reliability.sqlite'}", connect_args={"check_same_thread": False})
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(dbapi_connection, _):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")
        scoped = engine
        schema = None
        Base.metadata.create_all(scoped)
    try:
        yield sessionmaker(bind=scoped, expire_on_commit=False)
    finally:
        if schema:
            with engine.begin() as connection:
                connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        engine.dispose()


def new_batch(sessions, key="reliability"):
    return create_batch(sessions, jd=JD, material=MATERIAL, confirmed=False, idempotency_key=key)


def ready(sessions):
    batch = new_batch(sessions)
    assert process_batch_once(sessions, LocalProvider())
    assert process_guide_once(sessions, LocalProvider())
    assert process_guide_once(sessions, LocalProvider())
    assert get_batch(sessions, batch.id)["status"] == "ready"
    return batch


def expire(sessions, model, resource_id):
    with sessions() as session:
        session.execute(update(model).where(model.id == resource_id).values(lease_until=utcnow() - timedelta(seconds=1)))
        session.commit()


def edit(sessions, batch, kind="job", **changes):
    resume = get_batch(sessions, batch.id)["resumes"][kind]
    args = {name: deepcopy(resume[name]) for name in ("revision", "title", "summary", "items")}
    args.update(changes)
    return save_resume(sessions, batch.id, kind, **args)


@pytest.mark.parametrize("fails", [False, True])
def test_expired_batch_cannot_publish_success_or_failure(sessions, fails):
    batch = new_batch(sessions)
    class Expire(LocalProvider):
        def bundle(self, jd, material):
            expire(sessions, Batch, batch.id)
            if fails:
                raise RuntimeError("synthetic")
            return super().bundle(jd, material)
    assert process_batch_once(sessions, Expire())
    state = get_batch(sessions, batch.id)
    assert state["status"] == "running"
    assert state["resumes"] == {}
    assert list_executions(sessions, batch.id)[0]["status"] == "lease_expired"
    assert process_batch_once(sessions, LocalProvider())
    assert get_batch(sessions, batch.id)["status"] == "guides_pending"


@pytest.mark.parametrize("fails", [False, True])
def test_expired_guide_cannot_publish_success_or_failure(sessions, fails):
    batch = new_batch(sessions)
    process_batch_once(sessions, LocalProvider())
    class Expire(LocalProvider):
        def guide(self, kind, resume, plans, jd):
            with sessions() as session:
                guide = session.scalar(select(Guide).where(Guide.batch_id == batch.id, Guide.kind == kind))
                guide_id = guide.id
            expire(sessions, Guide, guide_id)
            if fails:
                raise RuntimeError("synthetic")
            return super().guide(kind, resume, plans, jd)
    assert process_guide_once(sessions, Expire())
    current = get_batch(sessions, batch.id)["guides"]["job"]
    assert current["status"] == "running"
    assert current["entries"] == []
    assert process_guide_once(sessions, LocalProvider())
    assert get_batch(sessions, batch.id)["guides"]["job"]["status"] == "ready"


@pytest.mark.parametrize("old_fails", [False, True])
def test_guide_aba_after_exhaustion_and_manual_retry_rejects_old_result(sessions, old_fails):
    batch = new_batch(sessions)
    process_batch_once(sessions, LocalProvider())
    class OldWorker(LocalProvider):
        def guide(self, kind, resume, plans, jd):
            with sessions() as session:
                guide_id = session.scalar(select(Guide.id).where(Guide.batch_id == batch.id, Guide.kind == kind))
            for _ in range(2):
                expire(sessions, Guide, guide_id)
                assert _claim_guide(sessions) is not None
            expire(sessions, Guide, guide_id)
            # Exhaust the job claim; blueprint may be claimed during this scan.
            other = _claim_guide(sessions)
            assert get_batch(sessions, batch.id)["guides"][kind]["status"] == "failed"
            retry_guide(sessions, batch.id, kind)
            replacement = _claim_guide(sessions)
            assert replacement is not None and replacement.resource_id == guide_id
            with sessions() as session:
                assert session.get(Guide, guide_id).attempts == 1
            if old_fails:
                raise RuntimeError("synthetic late failure")
            result = super().guide(kind, resume, plans, jd)
            result["entries"][0]["answer_outline"] = "这是旧执行者的晚返回，不应覆盖新的指南。"
            return result
    assert process_guide_once(sessions, OldWorker())
    current = get_batch(sessions, batch.id)["guides"]["job"]
    # The replacement is still running with the same attempt number. Old code
    # accepted this exact ABA state, so neither an old success nor failure may
    # change it before the replacement publishes its own result.
    assert current["status"] == "running"
    assert current["entries"] == []
    with sessions() as session:
        guide_id = session.scalar(select(Guide.id).where(Guide.batch_id == batch.id, Guide.kind == "job"))
    expire(sessions, Guide, guide_id)
    assert process_guide_once(sessions, LocalProvider())
    current = get_batch(sessions, batch.id)["guides"]["job"]
    assert current["status"] == "ready"
    assert "旧执行者" not in current["entries"][0]["answer_outline"]
    history = list_guide_revisions(sessions, batch.id, "job")
    assert len(history) == 2
    assert history[1]["status"] == "failed"
    assert history[1]["error_code"] == "ATTEMPTS_EXHAUSTED"
    with sessions() as session:
        tokens = session.scalars(select(TaskExecution.claim_token).where(TaskExecution.batch_id == batch.id)).all()
        assert len(tokens) == len(set(tokens))


@pytest.mark.parametrize("old_fails", [False, True])
def test_batch_aba_rejects_old_result_while_replacement_is_running(sessions, old_fails):
    batch = new_batch(sessions)
    class OldWorker(LocalProvider):
        def bundle(self, jd, material):
            for _ in range(2):
                expire(sessions, Batch, batch.id)
                assert _claim_batch(sessions) is not None
            expire(sessions, Batch, batch.id)
            assert _claim_batch(sessions) is None
            assert get_batch(sessions, batch.id)["status"] == "failed"
            retry_batch(sessions, batch.id)
            assert _claim_batch(sessions) is not None
            with sessions() as session:
                assert session.get(Batch, batch.id).attempts == 1
            if old_fails:
                raise RuntimeError("synthetic old failure")
            return super().bundle(jd, material)
    process_batch_once(sessions, OldWorker())
    current = get_batch(sessions, batch.id)
    assert current["status"] == "running"
    assert current["resumes"] == {}
    assert current["error_code"] is None
    expire(sessions, Batch, batch.id)
    assert process_batch_once(sessions, LocalProvider())
    assert get_batch(sessions, batch.id)["status"] == "guides_pending"


def test_batch_retry_preserves_failure_and_frozen_input(sessions):
    batch = new_batch(sessions)
    class Failure(LocalProvider):
        def bundle(self, jd, material):
            raise RuntimeError("synthetic")
    assert process_batch_once(sessions, Failure())
    assert get_batch(sessions, batch.id)["status"] == "failed"
    retry_batch(sessions, batch.id)
    assert process_batch_once(sessions, LocalProvider())
    records = list_executions(sessions, batch.id)
    assert [item["status"] for item in records] == ["failed", "succeeded"]
    assert records[0]["input_hash"] == records[1]["input_hash"]
    assert records[0]["attempt"] == records[1]["attempt"] == 1


def test_renewal_does_not_resurrect_expired_or_superseded_claim(sessions):
    batch = new_batch(sessions)
    claim = _claim_batch(sessions)
    assert renew_execution(sessions, claim.token)
    expire(sessions, Batch, batch.id)
    assert renew_execution(sessions, claim.token) is False
    next_claim = _claim_batch(sessions)
    assert next_claim.token != claim.token
    assert renew_execution(sessions, claim.token) is False
    assert renew_execution(sessions, next_claim.token)


def test_concurrent_claims_only_assign_batch_once_and_distinct_guides(sessions):
    batch = new_batch(sessions)
    barrier = Barrier(2)
    def claim_batch(_):
        barrier.wait(timeout=10)
        return _claim_batch(sessions)
    with ThreadPoolExecutor(max_workers=2) as executor:
        claims = list(executor.map(claim_batch, range(2)))
    assert sum(claim is not None for claim in claims) == 1
    expire(sessions, Batch, batch.id)
    process_batch_once(sessions, LocalProvider())
    barrier = Barrier(2)
    def claim_guide(_):
        barrier.wait(timeout=10)
        return _claim_guide(sessions)
    with ThreadPoolExecutor(max_workers=2) as executor:
        guides = list(executor.map(claim_guide, range(2)))
    assert all(claim is not None for claim in guides)
    assert len({claim.resource_id for claim in guides}) == 2
    assert len({claim.token for claim in guides}) == 2


@pytest.mark.parametrize("different_input", [False, True])
def test_concurrent_idempotency_unique_conflict_returns_winner(sessions, different_input):
    barrier = Barrier(2)
    class RaceSession(Session):
        def commit(self):
            barrier.wait(timeout=10)
            super().commit()
    racing = sessionmaker(bind=sessions.kw["bind"], expire_on_commit=False, class_=RaceSession)
    def invoke(index):
        try:
            result = create_batch(racing, jd=JD, material=MATERIAL + (str(index) if different_input else ""), confirmed=False, idempotency_key="concurrent-key")
            return result.id
        except InputConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(invoke, range(2)))
    with sessions() as session:
        assert session.scalar(select(func.count()).select_from(Batch)) == 1
    if different_input:
        assert results.count("conflict") == 1
    else:
        assert results[0] == results[1]


def test_concurrent_resume_revision_compare_and_swap(sessions):
    batch = ready(sessions)
    resume = get_batch(sessions, batch.id)["resumes"]["job"]
    barrier = Barrier(2)
    def invoke(index):
        items = deepcopy(resume["items"])
        items[0]["text"] = f"虚构改稿{index}：本人整理装配图与零件清单，记录设计评审。"
        barrier.wait(timeout=10)
        try:
            save_resume(sessions, batch.id, "job", revision=1, title=resume["title"], summary=resume["summary"], items=items)
            return "saved"
        except InputConflict:
            return "conflict"
    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(invoke, range(2)))
    assert sorted(outcomes) == ["conflict", "saved"]
    assert [row["revision"] for row in list_revisions(sessions, batch.id, "job")] == [2, 1]


def test_edit_derives_bound_plan_and_keeps_other_version_and_history(sessions):
    batch = ready(sessions)
    before = get_batch(sessions, batch.id)
    items = deepcopy(before["resumes"]["job"]["items"])
    items[0]["text"] = "虚构改稿：本人使用尺寸链检查装配间隙，形成复核清单。"
    after = edit(sessions, batch, items=items)
    assert after["resumes"]["blueprint"] == before["resumes"]["blueprint"]
    assert after["guides"]["blueprint"] == before["guides"]["blueprint"]
    assert after["resumes"]["job"]["plans"][0]["actions"] == [items[0]["text"]]
    old = get_resume_revision(sessions, batch.id, "job", 1)
    assert old["plans"] == before["resumes"]["job"]["plans"]
    old_guide = get_guide_revision(sessions, batch.id, "job", 1)
    assert old_guide["resume"]["items"] == before["resumes"]["job"]["items"]
    retry_guide(sessions, batch.id, "job")
    process_guide_once(sessions, LocalProvider())
    history = list_guide_revisions(sessions, batch.id, "job")
    assert [row["resume_revision"] for row in history] == [2, 1]
    assert history[1] == old_guide


def test_added_project_has_distinct_plan_and_stable_item_id(sessions):
    batch = ready(sessions)
    state = get_batch(sessions, batch.id)
    items = deepcopy(state["resumes"]["job"]["items"])
    items.append({"id": "", "plan_key": "", "text": "虚构新项目：整理传感器样例数据并形成检查说明。", "confirm": False, "achieved": False})
    after = edit(sessions, batch, items=items)
    resume = after["resumes"]["job"]
    added = resume["items"][-1]
    assert added["id"]
    assert added["plan_key"] != "user_added"
    assert added["plan_key"] in {plan["key"] for plan in resume["plans"]}
    assert added["verification_status"] == "user_unconfirmed"
    assert added["achieved"] is False
    assert after["resumes"]["blueprint"] == state["resumes"]["blueprint"]


@pytest.mark.parametrize("change", ["completion", "confirmation", "evidence", "resources", "plan"])
def test_state_only_changes_stale_guide(sessions, change):
    batch = ready(sessions)
    before = get_batch(sessions, batch.id)
    resume = before["resumes"]["job"]
    items = deepcopy(resume["items"])
    args = {"items": items}
    if change == "completion":
        items[0].update(confirm=True, achieved=True)
    elif change == "confirmation":
        items[0]["confirm"] = True
    elif change in {"evidence", "resources"}:
        items[0][change] = ["虚构记录：评审清单" if change == "evidence" else "可使用课程实验器材"]
    else:
        plans = deepcopy(resume["plans"])
        plans[0]["verification"] = "通过虚构尺寸链复核表逐项验证。"
        args["plans"] = plans
    after = edit(sessions, batch, **args)
    assert after["resumes"]["job"]["items"][0]["text"] == resume["items"][0]["text"]
    assert after["guides"]["job"]["status"] == "stale"
    assert after["guides"]["blueprint"]["status"] == "ready"


def test_confirmation_is_explicit_per_item_and_can_be_revoked(sessions):
    batch = ready(sessions)
    items = get_batch(sessions, batch.id)["resumes"]["job"]["items"]
    items[0].update(verification_status="user_confirmed", achieved=True)
    with pytest.raises(ValueError):
        edit(sessions, batch, items=items)
    items[0]["confirm"] = True
    confirmed = edit(sessions, batch, items=items)
    assert confirmed["resumes"]["job"]["items"][0]["achieved"] is True
    items = deepcopy(confirmed["resumes"]["job"]["items"])
    items[0].update(confirm=False, achieved=False)
    revoked = edit(sessions, batch, items=items)
    assert revoked["resumes"]["job"]["items"][0]["verification_status"] == "user_unconfirmed"
    assert revoked["resumes"]["job"]["items"][0]["achieved"] is False
    assert get_resume_revision(sessions, batch.id, "job", 2)["items"][0]["achieved"] is True


def test_pure_lock_edit_keeps_guide_binding_and_semantic_hash(sessions):
    batch = ready(sessions)
    before = get_batch(sessions, batch.id)
    items = deepcopy(before["resumes"]["job"]["items"])
    items[0]["locked"] = True
    after = edit(sessions, batch, items=items)
    assert after["resumes"]["job"]["revision"] == 2
    assert after["resumes"]["job"]["semantic_hash"] == before["resumes"]["job"]["semantic_hash"]
    guide = after["guides"]["job"]
    assert guide["status"] == "ready"
    assert guide["resume_revision"] == 1
    assert guide["applies_to_revision"] == 2
    assert get_guide_revision(sessions, batch.id, "job", 1)["resume_revision"] == 1
    changed = deepcopy(after["resumes"]["job"]["items"])
    changed[0]["text"] = "试图改变锁定内容，应拒绝保存。"
    with pytest.raises(InputConflict):
        edit(sessions, batch, items=changed)


def test_lock_only_save_while_guide_running_preserves_frozen_input(sessions):
    batch = new_batch(sessions)
    process_batch_once(sessions, LocalProvider())
    class LockDuringCall(LocalProvider):
        def guide(self, kind, resume, plans, jd):
            assert "locked" not in resume["items"][0]
            state = get_batch(sessions, batch.id)["resumes"][kind]
            items = deepcopy(state["items"])
            items[0].update(locked=True, confirm=False, achieved=False)
            edit(sessions, batch, kind=kind, items=items)
            assert "locked" not in resume["items"][0]
            return super().guide(kind, resume, plans, jd)
    process_guide_once(sessions, LockDuringCall())
    guide = get_batch(sessions, batch.id)["guides"]["job"]
    assert guide["status"] == "ready"
    assert guide["resume_revision"] == 1
    assert guide["applies_to_revision"] == 2


@pytest.mark.parametrize("fails", [False, True])
def test_content_edit_while_guide_running_rejects_late_result(sessions, fails):
    batch = new_batch(sessions)
    process_batch_once(sessions, LocalProvider())
    class ChangeDuringCall(LocalProvider):
        def guide(self, kind, resume, plans, jd):
            items = deepcopy(resume["items"])
            items[0]["text"] = "虚构改稿：本人整理尺寸复核清单并说明检查边界。"
            edit(sessions, batch, kind=kind, items=items)
            if fails:
                raise RuntimeError("synthetic late failure")
            return super().guide(kind, resume, plans, jd)
    process_guide_once(sessions, ChangeDuringCall())
    guide = get_batch(sessions, batch.id)["guides"]["job"]
    assert guide["status"] == "stale"
    assert guide["entries"] == []
    assert list_guide_revisions(sessions, batch.id, "job")[0]["status"] == "superseded"
    assert list_executions(sessions, batch.id)[-1]["status"] == "superseded"


def test_multiple_failed_guide_retries_preserve_failure_history(sessions):
    batch = ready(sessions)
    class Failure(LocalProvider):
        def guide(self, *args):
            raise RuntimeError("synthetic")
    for _ in range(2):
        retry_guide(sessions, batch.id, "job")
        process_guide_once(sessions, Failure())
    retry_guide(sessions, batch.id, "job")
    process_guide_once(sessions, LocalProvider())
    assert [row["status"] for row in list_guide_revisions(sessions, batch.id, "job")] == ["ready", "failed", "failed", "ready"]
    failures = [row for row in list_executions(sessions, batch.id) if row["error_code"] == "GUIDE_FAILED"]
    assert len(failures) == 2
    assert all(row["attempt"] == 1 for row in failures)


def test_deleted_batch_rejects_late_worker_and_cascades_private_history(sessions):
    batch = new_batch(sessions)
    class DeleteDuringGeneration(LocalProvider):
        def bundle(self, jd, material):
            with sessions() as session:
                session.execute(delete(Batch).where(Batch.id == batch.id))
                session.commit()
            return super().bundle(jd, material)
    assert process_batch_once(sessions, DeleteDuringGeneration())
    with sessions() as session:
        for model in (Batch, Resume, ResumeRevision, Guide, GuideRevision, TaskExecution):
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_batch_input_fingerprint_change_rejects_late_publication(sessions):
    batch = new_batch(sessions)
    class ChangeInput(LocalProvider):
        def bundle(self, jd, material):
            with sessions() as session:
                session.execute(update(Batch).where(Batch.id == batch.id).values(input_hash="0" * 64))
                session.commit()
            return super().bundle(jd, material)
    process_batch_once(sessions, ChangeInput())
    assert get_batch(sessions, batch.id)["resumes"] == {}
    assert list_executions(sessions, batch.id)[0]["status"] == "superseded"
