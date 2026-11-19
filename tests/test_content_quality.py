"""Content-quality signals: fabricated citations, ungrounded numbers, gap
commentary, guide field leaks, and the achieved-status contract.

All inputs are fictional. These tests pin the deterministic layer that stands
between model drafts and what a user might submit.
"""
from __future__ import annotations

from io import BytesIO

from docx import Document
from pypdf import PdfReader
import pytest

from app import files
from app.content import gap_phrases, normalize_bundle, normalize_guide, unverified_numbers
from app.providers import LocalProvider

JD = "机械结构设计工程师\n负责装配结构设计、图纸表达和可制造性评审。\n要求说明设计约束和验证方法。"
MATERIAL = "毕业项目：绘制装配图并整理零部件清单，完成课程设计汇报。"


def test_confirmed_verbatim_source_allows_achieved():
    raw = LocalProvider().bundle(JD, MATERIAL)
    raw["job"]["items"][0]["text"] = MATERIAL
    result = normalize_bundle(raw, JD, MATERIAL, True)
    job_item = result["resumes"]["job"]["items"][0]
    assert job_item["achieved"] is True
    assert job_item["source_status"] == "user_confirmed"
    assert job_item["verification_status"] == "ai_generated"
    assert job_item["flags"] == []
    assert result["resumes"]["blueprint"]["items"][0]["achieved"] is False


def test_identical_visible_resume_copy_is_rejected_despite_version_specific_ids():
    raw = LocalProvider().bundle(JD, MATERIAL)
    raw["blueprint"] = {
        "title": raw["job"]["title"],
        "summary": raw["job"]["summary"],
        "items": [dict(item) for item in raw["job"]["items"]],
    }
    with pytest.raises(ValueError, match="dual resumes identical"):
        normalize_bundle(raw, JD, MATERIAL, False)


def test_identical_resume_body_cannot_pass_by_changing_only_the_title():
    raw = LocalProvider().bundle(JD, MATERIAL)
    raw["blueprint"]["title"] = "完全不同的标题"
    raw["blueprint"]["summary"] = raw["job"]["summary"]
    raw["blueprint"]["items"] = [dict(item) for item in raw["job"]["items"]]
    with pytest.raises(ValueError, match="dual resumes identical"):
        normalize_bundle(raw, JD, MATERIAL, False)


def test_whitespace_only_resume_body_is_rejected_before_trimming_can_bypass_length():
    raw = LocalProvider().bundle(JD, MATERIAL)
    raw["job"]["items"][0]["text"] = " " * 12
    with pytest.raises(ValueError):
        normalize_bundle(raw, JD, MATERIAL, False)


def test_resume_fields_are_trimmed_before_storage():
    raw = LocalProvider().bundle(JD, MATERIAL)
    raw["job"]["items"][0]["text"] = "  已完成课程项目验证并整理项目记录。  "
    result = normalize_bundle(raw, JD, MATERIAL, False)
    assert result["resumes"]["job"]["items"][0]["text"] == "已完成课程项目验证并整理项目记录。"


@pytest.mark.parametrize("field", ["target_evidence", "background", "task", "outcome"])
def test_nested_required_plan_text_rejects_whitespace_only(field):
    raw = LocalProvider().bundle(JD, MATERIAL)
    raw["plans"][0][field] = " " * 12
    with pytest.raises(ValueError):
        normalize_bundle(raw, JD, MATERIAL, False)


def test_nested_action_steps_reject_whitespace_only_values():
    raw = LocalProvider().bundle(JD, MATERIAL)
    raw["plans"][0]["actions"] = ["  "]
    with pytest.raises(ValueError, match="plan actions must not be blank"):
        normalize_bundle(raw, JD, MATERIAL, False)


def test_confirmed_partial_quote_cannot_confirm_added_outcome():
    raw = LocalProvider().bundle(JD, MATERIAL)
    raw["job"]["items"][0].update(
        text=MATERIAL + "主导可制造性评审，将零部件数量减少 45%。",
        source_quote=MATERIAL,
        achieved=True,
    )
    result = normalize_bundle(raw, JD, MATERIAL, True)
    job_item = result["resumes"]["job"]["items"][0]
    assert job_item["source_quote"] == MATERIAL
    assert job_item["source_status"] == "user_confirmed"
    assert job_item["verification_status"] == "ai_generated"
    assert job_item["achieved"] is False
    assert job_item["flags"] == ["achievement_unverified"]
    assert result["quality"]["flagged_items"] == 1
    assert unverified_numbers(job_item["text"], JD, MATERIAL) == ["45%"]


def test_verbatim_source_does_not_complete_item_without_model_claim():
    raw = LocalProvider().bundle(JD, MATERIAL)
    raw["job"]["items"][0].update(text=MATERIAL, achieved=False)
    job_item = normalize_bundle(raw, JD, MATERIAL, True)["resumes"]["job"]["items"][0]
    assert job_item["achieved"] is False
    assert job_item["flags"] == []


def test_unconfirmed_material_blocks_and_flags_achievement_claim():
    raw = LocalProvider().bundle(JD, MATERIAL)
    raw["job"]["items"][0]["text"] = MATERIAL
    result = normalize_bundle(raw, JD, MATERIAL, False)
    job_item = result["resumes"]["job"]["items"][0]
    assert job_item["achieved"] is False
    assert job_item["source_status"] == "user_unconfirmed"
    assert "achievement_unverified" in job_item["flags"]


def test_blueprint_model_claim_stays_unfinished_even_with_confirmed_verbatim_source():
    provider = LocalProvider()
    raw = provider.bundle(JD, MATERIAL)
    raw["blueprint"]["items"][0].update(text=MATERIAL, source_quote=MATERIAL, achieved=True)
    result = normalize_bundle(raw, JD, MATERIAL, True)
    blueprint = result["resumes"]["blueprint"]
    item = blueprint["items"][0]
    assert item["text"] == MATERIAL
    assert item["source_quote"] == MATERIAL
    assert item["source_status"] == "user_confirmed"
    assert item["verification_status"] == "ai_generated"
    assert item["achieved"] is False
    assert item["flags"] == ["achievement_unverified"]

    guide = provider.guide("blueprint", blueprint, result["plans"], JD)
    assert "你会如何" in normalize_guide(guide, blueprint, "blueprint")[0]["question"]
    guide["entries"][0]["question"] = "请介绍一次你如何完成装配图设计？"
    with pytest.raises(ValueError, match="implies completed experience"):
        normalize_guide(guide, blueprint, "blueprint")


@pytest.mark.parametrize("answer", [
    "我当时已完成工厂验证并将良率提升45%。",
    "我曾经主导供应商询价并提高了交付效率。",
])
def test_unfinished_blueprint_rejects_first_person_past_achievement_in_answer(answer):
    resume = {"items": [{"id": "blueprint-1", "achieved": False}]}
    raw = {"entries": [{
        "item_index": 0,
        "question": "如何说明这项规划？",
        "answer_outline": answer,
        "contribution_boundary": "说明本人尚未实际完成该项目。",
        "verification": "后续保存工厂验证记录。",
        "practice_step": "准备验证方案。",
    }]}
    with pytest.raises(ValueError, match="answer claims unverified past experience"):
        normalize_guide(raw, resume, "blueprint")


@pytest.mark.parametrize("answer", [
    "我还没有完成工厂验证，未来会按计划收集良率数据。",
    "如果负责工厂验证，我会先制定方案，再记录良率变化。",
    "我计划完成验证并整理记录，当前尚未实施。",
])
def test_unfinished_blueprint_accepts_negated_future_and_conditional_answer(answer):
    resume = {"items": [{"id": "blueprint-1", "achieved": False}]}
    raw = {"entries": [{
        "item_index": 0,
        "question": "如何说明这项规划？",
        "answer_outline": answer,
        "contribution_boundary": "区分规划与实际完成情况。",
        "verification": "后续保存验证记录。",
        "practice_step": "准备验证方案。",
    }]}
    assert normalize_guide(raw, resume, "blueprint")[0]["answer_outline"] == answer


def test_unfinished_blueprint_accepts_quoted_past_claim_as_a_warning_example():
    resume = {"items": [{"id": "blueprint-1", "achieved": False}]}
    answer = "不要说“我当时已完成工厂验证”。应说明验证仍在计划中。"
    raw = {"entries": [{
        "item_index": 0,
        "question": "如何说明这项规划？",
        "answer_outline": answer,
        "contribution_boundary": "区分规划与实际完成情况。",
        "verification": "后续保存验证记录。",
        "practice_step": "准备验证方案。",
    }]}
    assert normalize_guide(raw, resume, "blueprint")[0]["answer_outline"] == answer


def test_completed_blueprint_may_discuss_verified_past_experience():
    resume = {"items": [{"id": "blueprint-1", "achieved": True}]}
    raw = {"entries": [{
        "item_index": 0,
        "question": "如何说明已完成的验证？",
        "answer_outline": "我当时已完成工厂验证并整理良率记录。",
        "contribution_boundary": "区分本人工作与团队结论。",
        "verification": "展示验证记录。",
        "practice_step": "准备记录摘要。",
    }]}
    assert normalize_guide(raw, resume, "blueprint")[0]["answer_outline"].startswith("我当时")


def test_fabricated_quote_is_dropped_flagged_and_counted():
    raw = LocalProvider().bundle(JD, MATERIAL)
    raw["job"]["items"][0]["source_quote"] = "用户主导过完整的自动化产线设计"
    result = normalize_bundle(raw, JD, MATERIAL, True)
    item = result["resumes"]["job"]["items"][0]
    assert item["source_quote"] == ""
    assert item["source_status"] == "none"
    assert "dropped_source_quote" in item["flags"]
    assert result["quality"]["dropped_source_quotes"] == 1
    assert result["quality"]["flagged_items"] == 1


def test_unverified_numbers_only_flag_digits_absent_from_inputs():
    text = "搭建 3 层测试环境，将缺陷率降低 45%。"
    assert unverified_numbers(text, "负责 3 层架构设计。", MATERIAL) == ["45%"]
    assert unverified_numbers("完成课程设计汇报。", JD, MATERIAL) == []


def test_gap_phrases_are_detected_for_job_version_display():
    assert gap_phrases("本人不具备量产经验，尚未完成验证，需要完善。") == ["不具备", "尚未", "需要完善"]
    assert gap_phrases("负责装配结构设计并输出图纸。") == []


def test_guide_rejects_internal_field_names():
    resume = {"items": [{"id": "job-1", "achieved": False}]}
    raw = {"entries": [{
        "item_index": 0,
        "question": "如何说明这项内容？",
        "answer_outline": "该条目的 source_status 已确认。",
        "contribution_boundary": "区分本人与团队工作。",
        "verification": "展示记录。",
        "practice_step": "整理材料。",
    }]}
    with pytest.raises(ValueError, match="internal field"):
        normalize_guide(raw, resume, "job")


def test_guide_accepts_legitimate_english_and_priority_codes():
    # Mixed-language material may contain words like "achieved" or "P2"; only
    # snake_case program tokens are treated as leaks.
    resume = {"items": [{"id": "job-1", "achieved": False}]}
    raw = {"entries": [{
        "item_index": 0,
        "question": "如何处理 P2 级问题？",
        "answer_outline": "说明 achieved 之外的实际贡献。",
        "contribution_boundary": "区分本人与团队工作。",
        "verification": "展示记录。",
        "practice_step": "整理材料。",
    }]}
    entries = normalize_guide(raw, resume, "job")
    assert entries[0]["question"] == "如何处理 P2 级问题？"


def test_blueprint_exports_carry_submission_warning():
    resume = {"title": "虚构蓝图版", "summary": "仅用于导出提示测试。", "items": [{"text": "虚构蓝图要点，尚未完成。"}]}
    note = "蓝图 Plus · 包含尚未完成的项目规划，仅供准备参考，请勿作为已核实简历投递"

    html = files.render_html(resume, footer=note).decode()
    assert note in html
    assert "export-note" in html

    docx = Document(BytesIO(files.render_docx(resume, footer=note)))
    assert docx.paragraphs[-1].text == note

    pdf = PdfReader(BytesIO(files.render_pdf(resume, footer=note)))
    extracted = "".join(page.extract_text() for page in pdf.pages)
    assert "蓝图 Plus" in extracted.replace("\n", "")

    default_html = files.render_html(resume).decode()
    assert '<footer class="export-note">' not in default_html


def test_stall_detection_must_outlast_worst_model_call(monkeypatch, tmp_path):
    from app.runtime import ConfigurationError, validate_configuration

    monkeypatch.setenv("COACH_PROVIDER", "local")
    monkeypatch.setenv("COACH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COACH_TIMEOUT_SECONDS", "120")
    with pytest.raises(ConfigurationError, match="停滞"):
        validate_configuration()
    monkeypatch.setenv("COACH_TIMEOUT_SECONDS", "90")
    assert validate_configuration()["provider"] == "local"
