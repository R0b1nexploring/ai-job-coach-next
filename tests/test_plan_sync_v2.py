from copy import deepcopy

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db import Base, make_engine
from app.generation_v2 import LocalWriterV2
from app.models import GuideRevision, Resume, ResumeRevision
from app.service import InputConflict
from app.service_v2 import create_v2_batch, get_v2_batch, save_v2_resume
from app.worker_v2 import process_batch_v2_once, process_guide_v2_once


@pytest.fixture
def prepared(tmp_path, monkeypatch):
    monkeypatch.setenv("COACH_PROVIDER", "local")
    engine = make_engine(f"sqlite:///{tmp_path / 'plan-test.sqlite'}")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    batch = create_v2_batch(sessions, jd="虚构岗位负责工程记录整理、资料核对及相关协作工作。", material="",
                            candidates=[], header={}, resume_locale="zh-CN", guide_locale="zh-CN",
                            idempotency_key="fictional-shared-plan")
    class TwoEntries(LocalWriterV2):
        def bundle(self, request):
            result = super().bundle(request)
            project = next(s for s in result["blueprint"]["sections"] if s["type"] == "projects")
            second = deepcopy(project["entries"][0])
            second["temp_key"] = "second-entry"
            second["units"][0]["temp_key"] = "second-unit"
            second["units"][0]["text"] = "第二个虚构项目行动"
            project["entries"].append(second)
            return result
    assert process_batch_v2_once(sessions, lambda _: TwoEntries())
    assert process_guide_v2_once(sessions) and process_guide_v2_once(sessions)
    yield sessions, batch["id"]
    engine.dispose()


def test_shared_plan_sync_edit_add_delete_history_and_version_isolation(prepared):
    sessions, batch_id = prepared
    before = get_v2_batch(sessions, batch_id)
    blue = before["resumes"]["blueprint"]
    entries = next(s for s in blue["document"]["sections"] if s["type"] == "projects")["entries"]
    first, second = entries
    plan_key = first["plan_ref"]
    assert second["plan_ref"] == plan_key
    plan = next(p for p in blue["plans"] if p["key"] == plan_key)
    old_unit = first["units"][0]["unit_id"]
    save_v2_resume(sessions, batch_id=batch_id, kind="blueprint", expected_revision=1,
                   title=blue["document"]["title"], header=None,
                   unit_texts={old_unit: "修改后的虚构项目行动"},
                   add_units=[{"entry_id":second["entry_id"], "text":"新增加的虚构项目行动"}])
    after = get_v2_batch(sessions, batch_id)
    new_blue = after["resumes"]["blueprint"]
    changed_plan = next(p for p in new_blue["plans"] if p["key"] == plan_key)
    assert changed_plan["actions"] == ["修改后的虚构项目行动", "第二个虚构项目行动", "新增加的虚构项目行动"]
    assert [x["entry_id"] for x in changed_plan["revision_units"]] == [first["entry_id"], second["entry_id"], second["entry_id"]]
    for key in ("title", "background", "task", "outcome", "verification", "resource_constraints"):
        assert changed_plan[key] == plan[key]
    assert set(changed_plan["needs_review"]) >= {"background", "task", "outcome", "verification", "resource_constraints"}
    assert new_blue["revision"] == 2 and after["guides"]["blueprint"]["status"] == "stale"
    assert after["resumes"]["job"] == before["resumes"]["job"]
    assert after["guides"]["job"] == before["guides"]["job"]
    with sessions() as db:
        old = db.scalar(select(ResumeRevision).where(ResumeRevision.resume_id == blue["id"], ResumeRevision.revision == 1))
        fresh = db.scalar(select(ResumeRevision).where(ResumeRevision.resume_id == blue["id"], ResumeRevision.revision == 2))
        assert old.plans == blue["plans"] and fresh.plans == new_blue["plans"]
        historical_guide = db.scalar(select(GuideRevision).where(GuideRevision.batch_id == batch_id, GuideRevision.resume_revision == 1, GuideRevision.resume_snapshot["kind"].as_string() == "blueprint"))
        assert historical_guide.plans == blue["plans"]
    current_entries = next(s for s in new_blue["document"]["sections"] if s["type"] == "projects")["entries"]
    delete_id = current_entries[1]["units"][0]["unit_id"]
    save_v2_resume(sessions, batch_id=batch_id, kind="blueprint", expected_revision=2,
                   title=new_blue["document"]["title"], header=None, unit_texts={}, delete_unit_ids=[delete_id])
    final = get_v2_batch(sessions, batch_id)["resumes"]["blueprint"]
    assert next(p for p in final["plans"] if p["key"] == plan_key)["actions"] == ["修改后的虚构项目行动", "新增加的虚构项目行动"]


def test_plan_explicit_edits_preserve_facts_and_require_action_consistency(prepared):
    sessions, batch_id = prepared
    before = get_v2_batch(sessions, batch_id)
    blue = before["resumes"]["blueprint"]
    entries = next(s for s in blue["document"]["sections"] if s["type"] == "projects")["entries"]
    key = entries[0]["plan_ref"]
    fields = {"background":"用户填写的虚构背景", "task":"用户填写的虚构任务", "outcome":"待核对的虚构产物",
              "verification":"用户填写的核对方式", "resource_constraints":["仅使用虚构资料"]}
    save_v2_resume(sessions, batch_id=batch_id, kind="blueprint", expected_revision=1,
                   title=blue["document"]["title"], unit_texts={}, header=None, plan_updates={key:fields})
    after = get_v2_batch(sessions, batch_id)
    plan = next(p for p in after["resumes"]["blueprint"]["plans"] if p["key"] == key)
    assert all(plan[field] == value for field,value in fields.items())
    assert all(plan["field_authorship"][field] == "user_edited_unconfirmed" for field in fields)
    assert set(fields) <= set(plan["needs_review"])
    assert after["guides"]["blueprint"]["status"] == "stale"
    assert after["resumes"]["job"] == before["resumes"]["job"]
    with pytest.raises(ValueError, match="正文一致"):
        save_v2_resume(sessions, batch_id=batch_id, kind="blueprint", expected_revision=2,
                       title=blue["document"]["title"], unit_texts={}, header=None,
                       plan_updates={key:{"actions":["与正文冲突"]}})
    with pytest.raises(InputConflict):
        save_v2_resume(sessions, batch_id=batch_id, kind="blueprint", expected_revision=1,
                       title=blue["document"]["title"], unit_texts={}, header=None, plan_updates={key:fields})
    assert get_v2_batch(sessions, batch_id)["resumes"]["blueprint"]["revision"] == 2


def test_unchanged_project_plan_remains_byte_equivalent(prepared):
    sessions, batch_id = prepared
    blue = get_v2_batch(sessions, batch_id)["resumes"]["blueprint"]
    saved = save_v2_resume(sessions, batch_id=batch_id, kind="blueprint", expected_revision=1,
                           title=blue["document"]["title"], unit_texts={}, header={"name":"虚构姓名"})
    assert saved["revision"] == 2
    after = get_v2_batch(sessions, batch_id)["resumes"]["blueprint"]
    assert after["plans"] == blue["plans"]


def test_deleting_all_project_units_keeps_unknown_actions_without_invention(prepared):
    sessions, batch_id = prepared
    blue = get_v2_batch(sessions, batch_id)["resumes"]["blueprint"]
    entries = next(section for section in blue["document"]["sections"] if section["type"] == "projects")["entries"]
    key = entries[0]["plan_ref"]
    old_plan = next(plan for plan in blue["plans"] if plan["key"] == key)
    unit_ids = [unit["unit_id"] for entry in entries for unit in entry["units"]]
    save_v2_resume(sessions, batch_id=batch_id, kind="blueprint", expected_revision=1,
                   title=blue["document"]["title"], unit_texts={}, header=None, delete_unit_ids=unit_ids)
    plan = next(plan for plan in get_v2_batch(sessions, batch_id)["resumes"]["blueprint"]["plans"] if plan["key"] == key)
    assert plan["actions"] == [] and plan["revision_units"] == []
    assert "actions" in plan["needs_review"]
    assert (plan["outcome"], plan["verification"]) == (old_plan["outcome"], old_plan["verification"])


def test_unreferenced_unknown_and_oversized_plan_updates_rejected(prepared):
    sessions, batch_id = prepared
    initial = get_v2_batch(sessions, batch_id)
    blue, job = initial["resumes"]["blueprint"], initial["resumes"]["job"]
    key = next(section for section in blue["document"]["sections"] if section["type"] == "projects")["entries"][0]["plan_ref"]
    for kind, data, update in (
        ("job", job, {key:{"background":"不应编辑其他版方案"}}),
        ("blueprint", blue, {"missing":{"task":"不存在"}}),
        ("blueprint", blue, {key:{"status":"completed"}}),
        ("blueprint", blue, {key:{"resource_constraints":["x"] * 101}}),
    ):
        with pytest.raises(ValueError):
            save_v2_resume(sessions, batch_id=batch_id, kind=kind, expected_revision=1,
                           title=data["document"]["title"], unit_texts={}, header=None, plan_updates=update)
    after = get_v2_batch(sessions, batch_id)
    assert after["resumes"] == initial["resumes"] and after["guides"] == initial["guides"]


def test_summary_linked_to_plan_does_not_become_an_action():
    from app.service_v2 import _sync_v2_plans
    unit = {"unit_id":"u", "text":"摘要原文", "claim_refs":[], "authorship":"ai_proposed", "completion":"unknown", "purpose":"summary", "plan_ref":"p", "locked":False}
    before = {"summary": unit, "sections": []}
    after = deepcopy(before)
    after["summary"]["text"] = "摘要修改"
    plan = {"key":"p", "actions":["原有步骤"], "background":"原背景", "task":"原任务", "outcome":"原结果", "verification":"原检查", "resource_constraints":[]}
    result = _sync_v2_plans([plan], before, after, None)[0]
    assert result["actions"] == ["原有步骤"]
    assert "background" in result["needs_review"]


def test_sending_unchanged_plan_form_does_not_create_a_revision(prepared):
    sessions, batch_id = prepared
    before = get_v2_batch(sessions, batch_id)
    blue = before["resumes"]["blueprint"]
    key = next(section for section in blue["document"]["sections"] if section["type"] == "projects")["entries"][0]["plan_ref"]
    plan = next(plan for plan in blue["plans"] if plan["key"] == key)
    fields = {field: deepcopy(plan[field]) for field in ("title", "background", "task", "outcome", "verification", "resource_constraints")}
    response = save_v2_resume(sessions, batch_id=batch_id, kind="blueprint", expected_revision=1,
                              title=blue["document"]["title"], unit_texts={}, header=None, plan_updates={key:fields})
    assert response == {"revision":1, "changed":False}
    after = get_v2_batch(sessions, batch_id)
    assert after["resumes"] == before["resumes"] and after["guides"] == before["guides"]
