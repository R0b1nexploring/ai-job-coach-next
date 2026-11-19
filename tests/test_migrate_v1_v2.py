from copy import deepcopy

import pytest
from app.migrate_v1_v2 import prepare_v1_upgrade


def sample():
    resume = {"title": "工程师", "summary": "虚构项目背景", "header": {"name": "示例"}, "items": [
        {"id": "old-1", "plan_key": "p1", "text": "整理项目资料", "source_quote": "项目资料", "source_status": "user_confirmed", "verification_status": "ai_generated", "achieved": False},
        {"id": "old-2", "plan_key": "p2", "text": "完成虚构记录", "source_status": "user_unconfirmed", "verification_status": "user_confirmed", "achieved": True},
        {"id": "old-3", "plan_key": "p1", "text": "继续整理内容", "achieved": False},
    ]}
    plans = [{"key": "p1", "title": "甲方案", "background": "虚构背景", "task": "虚构任务", "actions": ["资料整理"], "outcome": "预期成果", "verification": "核验记录", "resource_constraints": ["使用虚构资料"]},
             {"key": "p2", "title": "乙方案", "background": "另一背景", "task": "另一任务", "actions": ["准备记录"], "outcome": "另一成果", "verification": "核验文件"}]
    return resume, plans


def prepare(resume, plans, **kwargs):
    return prepare_v1_upgrade(legacy_resume=resume, legacy_plans=plans, source_resume_id="old-resume", source_revision=3, private_scope="local", kind="blueprint", **kwargs)


def test_explicit_upgrade_preserves_old_data_states_order_and_complete_plans():
    resume, plans = sample(); old_resume, old_plans = deepcopy(resume), deepcopy(plans)
    result = prepare(resume, plans)
    assert resume == old_resume and plans == old_plans
    entries = result.document["sections"][0]["entries"]
    assert [e["units"][0]["text"] for e in entries] == [item["text"] for item in resume["items"]]
    assert [e["units"][0]["completion"] for e in entries] == ["unknown", "completed", "unknown"]
    assert entries[0]["plan_ref"] == entries[2]["plan_ref"] != entries[1]["plan_ref"]
    assert result.plans[0]["actions"] == plans[0]["actions"] and result.plans[0]["resource_constraints"] == plans[0]["resource_constraints"]
    assert result.provenance["source_revision"] == 3
    assert result.provenance["items"][0]["source_status"] == "user_confirmed"
    assert result.provenance["items"][0]["legacy_achieved"] is False
    assert result.fact_snapshot.claims == () and entries[0]["units"][0]["claim_refs"] == []
    assert result.header == resume["header"]
    assert "verified" not in str(result.provenance)


def test_stable_id_map_reuse_rejects_changed_source_or_tampering():
    resume, plans = sample(); result = prepare(resume, plans)
    repeated = prepare(resume, plans, id_map=result.id_map)
    assert repeated == result
    resume["items"][0]["text"] = "改动后的文字"
    with pytest.raises(ValueError, match="does not match"):
        prepare(resume, plans, id_map=result.id_map)
    resume, plans = sample(); bad = deepcopy(result.id_map)
    bad["ids"]["summary"] = bad["ids"]["snapshot"]
    with pytest.raises(ValueError, match="invalid ID map"):
        prepare(resume, plans, id_map=bad)


def test_missing_data_and_nonready_state_never_fabricated():
    resume, plans = sample(); del plans[0]
    with pytest.raises(ValueError, match="bound plan"):
        prepare(resume, plans)
    resume, plans = sample(); resume["status"] = "running"
    with pytest.raises(ValueError, match="not ready"):
        prepare(resume, plans)
    resume, plans = sample(); resume["summary"] = ""
    with pytest.raises(ValueError, match="incomplete"):
        prepare(resume, plans)
    resume, plans = sample(); resume["items"][0]["achieved"] = "false"
    with pytest.raises(ValueError, match="completion"):
        prepare(resume, plans)


def test_missing_legacy_completion_remains_unknown_without_original_value():
    resume, plans = sample(); del resume["items"][0]["achieved"]
    upgrade = prepare(resume, plans)
    assert upgrade.document["sections"][0]["entries"][0]["units"][0]["completion"] == "unknown"
    assert upgrade.provenance["items"][0]["legacy_achieved"] is None
