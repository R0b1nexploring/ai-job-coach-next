"""Adversarial field boundaries for AI-generated review and repair responses."""
from copy import deepcopy

import pytest

from app.content import normalize_bundle
from app.content_review import apply_repair, validate_review
from app.providers import LocalProvider, REVIEW_SYSTEM


JD = "虚构岗位：整理课程项目的图纸、零件清单和验证过程。"
MATERIAL = "虚构课程项目中绘制了装配图，整理零件清单并记录检查过程。"


@pytest.fixture
def content():
    return normalize_bundle(LocalProvider().bundle(JD, MATERIAL), JD, MATERIAL, False)


def review_for(content, *, scope="job", target_id="job-1", field="text"):
    value = content["plans"][0][field] if scope == "plan" else content["resumes"][scope]["items"][0][field]
    return {"assessed_scopes": ["job", "blueprint", "plans"], "issues": [{
        "id": "issue-1", "severity": "error", "code": "causal_gap",
        "targets": [{"scope": scope, "target_id": target_id, "field": field,
                     "quote": value[0] if isinstance(value, list) else value}],
        "diagnosis": "需要说明行动和产物的具体对应关系。", "instruction": "补充该字段内的可检查行动。", "jd_quote": "整理课程项目",
    }]}


@pytest.mark.parametrize("damage", ["missing_scope", "duplicate_scope", "duplicate_issue", "unknown_target", "wrong_quote", "wrong_jd_quote", "style_gate", "protected_field", "unknown_property"])
def test_invalid_or_incomplete_review_never_counts_as_passed(content, damage):
    raw = review_for(content)
    issue = raw["issues"][0]
    if damage == "missing_scope":
        raw["assessed_scopes"].pop()
    elif damage == "duplicate_scope":
        raw["assessed_scopes"] = ["job", "job", "plans"]
    elif damage == "duplicate_issue":
        raw["issues"].append(deepcopy(issue))
    elif damage == "unknown_target":
        issue["targets"][0]["target_id"] = "nonexistent"
    elif damage == "wrong_quote":
        issue["targets"][0]["quote"] = "草稿中没有这样的原句"
    elif damage == "wrong_jd_quote":
        issue["jd_quote"] = "需要十年商业软件开发经验"
    elif damage == "style_gate":
        issue["code"] = "wording"
    elif damage == "protected_field":
        issue["targets"][0]["field"] = "source_status"
    else:
        raw["approved"] = True
    with pytest.raises(ValueError):
        validate_review(raw, content, JD)


def test_duplicate_actions_target_remains_invalid(content):
    plan = content["plans"][0]
    raw = review_for(content, scope="plan", target_id=plan["key"], field="actions")
    duplicate = deepcopy(raw["issues"][0]["targets"][0])
    if len(plan["actions"]) > 1:
        duplicate["quote"] = plan["actions"][1]
    raw["issues"][0]["targets"].append(duplicate)
    with pytest.raises(ValueError, match="duplicate issue target"):
        validate_review(raw, content, JD)
    assert "只能出现一次" in REVIEW_SYSTEM


def edit_for(content):
    return {"issue_id": "issue-1", "scope": "job", "target_id": "job-1", "field": "text",
            "before": content["resumes"]["job"]["items"][0]["text"],
            "after": "结合课程装配图逐项记录尺寸检查，并说明零件清单与对应图纸的检查关系。"}


@pytest.mark.parametrize("damage", ["stale_before", "duplicate_edit", "suggestion", "locked_item", "empty_text", "identical_versions", "added_state"])
def test_rejected_patch_is_atomic_and_preserves_input(content, damage):
    issues = review_for(content)["issues"]
    edit = edit_for(content)
    raw = {"edits": [edit]}
    if damage == "stale_before":
        edit["before"] = "已经变更的旧正文"
    elif damage == "duplicate_edit":
        raw["edits"].append(deepcopy(edit))
    elif damage == "suggestion":
        issues[0]["severity"] = "suggestion"
    elif damage == "locked_item":
        content["resumes"]["job"]["items"][0]["locked"] = True
    elif damage == "empty_text":
        edit["after"] = " "
    elif damage == "identical_versions":
        content["resumes"]["job"]["summary"] = content["resumes"]["blueprint"]["summary"]
        edit["after"] = content["resumes"]["blueprint"]["items"][0]["text"]
    else:
        edit["achieved"] = True
    original = deepcopy(content)
    with pytest.raises(ValueError):
        apply_repair(raw, content, issues, jd=JD, material=MATERIAL, confirmed=False)
    assert content == original


def test_actions_patch_preserves_other_plan_fields_and_both_resumes(content):
    issues = review_for(content, scope="plan", target_id="project-1", field="actions")["issues"]
    raw = {"edits": [{"issue_id": "issue-1", "scope": "plan", "target_id": "project-1", "field": "actions",
                      "before": content["plans"][0]["actions"], "after": ["建立图纸与零件清单对应表", "记录不一致项及复核办法"]}]}
    result, _ = apply_repair(raw, content, issues, jd=JD, material=MATERIAL, confirmed=False)
    assert result["resumes"] == content["resumes"]
    assert {k: v for k, v in result["plans"][0].items() if k != "actions"} == {k: v for k, v in content["plans"][0].items() if k != "actions"}
    content["resumes"]["blueprint"]["items"][0]["locked"] = True
    with pytest.raises(ValueError):
        apply_repair(raw, content, issues, jd=JD, material=MATERIAL, confirmed=False)
