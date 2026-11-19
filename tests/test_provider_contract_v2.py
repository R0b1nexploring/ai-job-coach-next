from copy import deepcopy

import pytest
from pydantic import ValidationError

from app.provider_contract_v2 import (
    GuideRequest, WriterRequest, build_writer_request, build_guide_request, validate_bundle_output,
    validate_guide_output,
)


def writer_request(locale="en"):
    return WriterRequest.model_validate({"jd_text": "Fictional engineer role", "facts": [{"claim_id": "c1", "fact_type": "action", "value": "organized equipment notes", "confirmation": "unconfirmed", "completion": "unknown"}], "resume_locale": locale})


def bundle():
    def doc(kind, prefix):
        return {"kind": kind, "locale": "en", "title": "Engineer",
                "summary": {"temp_key": prefix + "-summary", "text": "Engineering work", "claim_refs": ["c1"], "purpose": "summary"},
                "sections": [{"temp_key": prefix + "-section", "type": "experience", "entries": [{"temp_key": prefix + "-entry", "units": [{"temp_key": prefix + "-unit", "text": "Organized equipment notes", "claim_refs": ["c1"], "purpose": "experience"}]}]}]}
    return {"job": doc("job", "j"), "blueprint": doc("blueprint", "b"), "plans": []}


class FakeWriter:
    def bundle(self, request):
        return bundle()

    def guide(self, request):
        return {"kind": request.kind, "locale": request.guide_locale,
                "entries": [{"unit_ids": [unit.unit_id], "question": "How would you explain this?", "answer_outline": "Describe the actual contribution."} for unit in request.units],
                "context_notes": []}


def test_fake_bundle_uses_explicit_languages_and_references_without_assigning_states():
    request = writer_request()
    result = validate_bundle_output(FakeWriter().bundle(request), request)
    assert result.job.kind == "job" and result.blueprint.kind == "blueprint"
    assert result.job.sections[0].entries[0].units[0].claim_refs == ("c1",)
    assert not hasattr(result.job.summary, "confirmation")
    assert not hasattr(result.job.summary, "completion")
    payload = bundle()
    payload["job"]["summary"]["confirmation"] = "user_confirmed"
    with pytest.raises(ValidationError):
        validate_bundle_output(payload, request)
    payload = bundle()
    payload["blueprint"]["sections"][0]["entries"][0]["units"][0]["completion"] = "completed"
    with pytest.raises(ValidationError):
        validate_bundle_output(payload, request)
    with pytest.raises(ValueError, match="locale"):
        validate_bundle_output(bundle(), writer_request("zh-CN"))


def test_rejects_unknown_references_missing_plan_and_duplicate_keys():
    request = writer_request()
    for mutate in (lambda b: b["job"]["summary"].update(claim_refs=["other"]),
                   lambda b: b["job"]["sections"][0].update(type="projects"),
                   lambda b: b["job"]["sections"][0].update(temp_key="j-summary"),
                   lambda b: b["job"].update(kind="blueprint")):
        raw = bundle()
        mutate(raw)
        with pytest.raises((ValueError, ValidationError)):
            validate_bundle_output(raw, request)


def test_guide_binding_coverage_and_empty_case():
    request = GuideRequest.model_validate({"resume_id": "r1", "resume_revision": 2, "semantic_hash": "f" * 64, "kind": "job", "guide_locale": "zh-CN", "jd_text": "虚构岗位", "requirement_ids": ["req1"], "units": [{"unit_id": "unit1", "text": "整理记录", "completion": "unknown"}]})
    result = validate_guide_output(FakeWriter().guide(request), request)
    assert result.locale == "zh-CN" and result.entries[0].unit_ids == ("unit1",)
    raw = FakeWriter().guide(request)
    raw["entries"][0]["unit_ids"] = ["other-version"]
    with pytest.raises(ValueError):
        validate_guide_output(raw, request)
    raw = FakeWriter().guide(request)
    raw["context_notes"] = [{"text": "准备说明", "requirement_ids": ["not-in-jd"]}]
    with pytest.raises(ValueError):
        validate_guide_output(raw, request)
    empty = request.model_copy(update={"units": ()})
    with pytest.raises(ValueError):
        validate_guide_output(FakeWriter().guide(empty), empty)
    validate_guide_output({"kind": "job", "locale": "zh-CN", "entries": [], "context_notes": [{"text": "列出可补充的课程经历", "requirement_ids": ["req1"]}]}, empty)


def test_writer_builder_uses_allowlist_and_rejects_sensitive_jd():
    from app.content_v2 import Claim, FactSnapshot
    snapshot = FactSnapshot(snapshot_id="s", private_scope="local", claims=(
        Claim(claim_id="c1", fact_type="action", value="整理设备记录"),
        Claim(claim_id="c2", fact_type="raw_material", value="虚构姓名探针参与项目"),
        Claim(claim_id="c3", fact_type="action", value="联系13800000000本人"),
    ))
    request = build_writer_request(jd_text="虚构工程岗位", snapshot=snapshot, resume_locale="zh-CN", private_header={"name": "虚构姓名探针"})
    assert [request._alias_to_claim[fact.claim_id] for fact in request.facts] == ["c1"]
    assert "c1" not in request.model_dump_json()
    for probe in ("13800000000", "虚构姓名探针", "raw_material"):
        assert probe not in request.model_dump_json()
    for jd in ("联系13800000000本人", "联系１３８００００００００本人", "联系人虚构姓名探针", "邮件probe@example.invalid"):
        with pytest.raises(ValueError, match="privacy"):
            build_writer_request(jd_text=jd, snapshot=snapshot, resume_locale="zh-CN", private_header={"name": "虚构姓名探针"})
    with pytest.raises(ValidationError):
        WriterRequest.model_validate({**request.model_dump(), "header": {"name": "虚构姓名探针"}})
    with pytest.raises(ValidationError):
        WriterRequest.model_validate({"jd_text": "虚构岗位", "facts": [{"claim_id": "x", "fact_type": "raw_material", "value": "任意原文", "confirmation": "unconfirmed", "completion": "unknown"}], "resume_locale": "zh-CN"})
    with pytest.raises(ValueError, match="privacy"):
        build_guide_request(private_header={"name": "虚构姓名探针"}, resume_id="r", resume_revision=1, semantic_hash="f" * 64, kind="job", guide_locale="zh-CN", jd_text="联系人虚构姓名探针", units=[])


def test_uuid4_ids_are_not_phones_but_known_private_values_still_rejected():
    from app.content_v2 import Claim, FactSnapshot
    app_id = "123456789abc4def8123456789012345"
    snapshot = FactSnapshot(snapshot_id="s", private_scope="local", claims=(Claim(claim_id=app_id, fact_type="action", value="整理设备记录"),))
    request = build_writer_request(jd_text="虚构岗位", snapshot=snapshot, private_header={}, resume_locale="zh-CN")
    assert request._alias_to_claim[request.facts[0].claim_id] == app_id
    assert app_id not in request.model_dump_json()
    guide = build_guide_request(private_header={}, resume_id=app_id, resume_revision=1, semantic_hash="f" * 64, kind="job", guide_locale="zh-CN", jd_text="虚构岗位", units=[{"unit_id": app_id, "text": "整理设备记录", "completion": "unknown"}])
    assert guide.units[0].unit_id == app_id
    # The resume identifier is local binding metadata and is excluded from
    # actual compatible provider input; matching a short header is not a leak.
    assert build_guide_request(private_header={"name": "1234567"}, resume_id=app_id, resume_revision=1, semantic_hash="f" * 64, kind="job", guide_locale="zh-CN", jd_text="虚构岗位", units=[]).resume_id == app_id


@pytest.mark.parametrize("field", ["unit_id", "requirement_id"])
def test_guide_binding_fields_reject_known_private_values(field):
    payload = {"resume_id": "r1", "resume_revision": 1, "semantic_hash": "f" * 64, "kind": "job", "guide_locale": "zh-CN", "jd_text": "虚构岗位", "units": [{"unit_id": "u1", "text": "整理记录", "completion": "unknown"}], "requirement_ids": ["req1"]}
    if field == "unit_id":
        payload["units"][0]["unit_id"] = "敏感姓名探针"
    elif field == "requirement_id":
        payload["requirement_ids"] = ["敏感姓名探针"]
    else:
        payload[field] = "敏感姓名探针"
    with pytest.raises(ValueError, match="privacy"):
        build_guide_request(private_header={"name": "敏感姓名探针"}, **payload)


def test_guide_semantic_hash_requires_lowercase_sha256_without_private_substring_false_positive():
    payload = {"resume_id": "r", "resume_revision": 1, "kind": "job", "guide_locale": "zh-CN", "jd_text": "虚构岗位", "units": []}
    for invalid in ("h", "A" * 64, "z" * 64, "林姓名"):
        with pytest.raises(ValueError):
            build_guide_request(private_header={}, semantic_hash=invalid, **payload)
    # Hash is opaque metadata; its digit runs must not be mistaken for phones.
    digest = "1234567890abcdef" * 4
    assert build_guide_request(private_header={}, semantic_hash=digest, **payload).semantic_hash == digest
    assert build_guide_request(private_header={"phone": "1234567890"}, semantic_hash=digest, **payload).semantic_hash == digest


def test_preview_and_writer_use_same_aliases_for_ca_and_hide_forged_private_id():
    from app.content_v2 import Claim, FactSnapshot
    from app.model_projection_v2 import project_facts_with_diagnostics
    fake = FactSnapshot(snapshot_id="s", private_scope="local", claims=(Claim(claim_id="candidate-0", fact_type="action", value="设备记录"),))
    real = FactSnapshot(snapshot_id="s", private_scope="local", claims=(Claim(claim_id="11111111222243338444555555555555", fact_type="action", value="设备记录"),))
    header = {"location": "CA"}
    preview = project_facts_with_diagnostics(fake, private_header=header, alias_identifiers=True)
    request = build_writer_request(jd_text="虚构工程岗位", snapshot=real, private_header=header, resume_locale="zh-CN")
    assert preview.diagnostics.excluded == ()
    assert [x["claim_id"] for x in preview.model_input["claims"]] == [fact.claim_id for fact in request.facts]
    forged = FactSnapshot(snapshot_id="s", private_scope="local", claims=(Claim(claim_id="probe@example.invalid", fact_type="action", value="设备记录"),))
    safe = build_writer_request(jd_text="虚构工程岗位", snapshot=forged, private_header={"email": "probe@example.invalid"}, resume_locale="zh-CN")
    assert "probe@example.invalid" not in safe.model_dump_json()
    assert safe._alias_to_claim[safe.facts[0].claim_id] == "probe@example.invalid"


@pytest.mark.parametrize(("section_type", "field", "fact_type"), [
    ("experience", "organization_ref", "employer"),
    ("experience", "role_ref", "job_title"),
    ("experience", "date_ref", "employment_date"),
    ("education", "school_ref", "school"),
    ("education", "major_ref", "major"),
    ("education", "degree_ref", "degree"),
    ("education", "date_ref", "education_date"),
])
def test_provider_display_references_are_type_checked_before_acceptance(section_type, field, fact_type):
    raw = bundle()
    entry = raw["job"]["sections"][0]["entries"][0]
    raw["job"]["sections"][0]["type"] = section_type
    entry[field] = "display"
    raw["blueprint"]["sections"][0]["type"] = section_type
    raw["blueprint"]["sections"][0]["entries"][0][field] = "display"
    req = writer_request().model_dump(mode="json")
    req["facts"].append({"claim_id": "display", "fact_type": fact_type, "value": "fictional",
                         "confirmation": "unconfirmed", "completion": "unknown"})
    validate_bundle_output(raw, WriterRequest.model_validate(req))
    req["facts"][-1]["fact_type"] = "action"
    with pytest.raises(ValueError, match="incompatible fact type"):
        validate_bundle_output(raw, WriterRequest.model_validate(req))


@pytest.mark.parametrize("field,text", [
    ("question", "请解释 unit_ids 对应的内容"),
    ("answer_outline", "单位文本标为 AI 改写，请确认。"),
    ("answer_outline", "该单元标记为模型生成。"),
    ("context", "来源标记说明该资料未核实。"),
    ("context", "请检查 source_types 与 field_authorship。"),
    ("context", "completion=completed"),
])
def test_guide_rejects_internal_terms_only_in_visible_prose(field, text):
    request = GuideRequest.model_validate({"resume_id": "r", "resume_revision": 1, "semantic_hash": "f" * 64,
        "kind": "job", "guide_locale": "zh-CN", "jd_text": "虚构岗位", "units": [{"unit_id": "u1", "text": "整理记录", "completion": "unknown"}]})
    output = FakeWriter().guide(request)
    if field == "context":
        output["context_notes"] = [{"text": text, "requirement_ids": []}]
    else:
        output["entries"][0][field] = text
    with pytest.raises(ValueError, match="internal metadata"):
        validate_guide_output(output, request)


def test_guide_allows_genuine_ai_skills_and_plain_uncertainty():
    request = GuideRequest.model_validate({"resume_id": "r", "resume_revision": 1, "semantic_hash": "f" * 64,
        "kind": "job", "guide_locale": "zh-CN", "jd_text": "虚构岗位需要 AI 和机器学习", "units": [{"unit_id": "u1", "text": "学习 AI 模型部署", "completion": "unknown"}]})
    result = validate_guide_output({"kind": "job", "locale": "zh-CN", "entries": [{"unit_ids": ["u1"],
        "question": "你如何使用 AI 辅助数据分析？", "answer_outline": "结合机器学习练习说明你的实际工作，尚未完成的部分先核对。"}],
        "context_notes": [{"text": "请确认训练和部署工作是否由你完成。", "requirement_ids": []}]}, request)
    assert "AI" in result.entries[0].question


def test_guide_allows_natural_english_completion_and_ai_skills():
    request = GuideRequest.model_validate({"resume_id": "r", "resume_revision": 1, "semantic_hash": "f" * 64,
        "kind": "job", "guide_locale": "en", "jd_text": "Fictional AI role", "units": [{"unit_id": "u1", "text": "AI model testing", "completion": "unknown"}]})
    result = validate_guide_output({"kind": "job", "locale": "en", "entries": [{"unit_ids": ["u1"],
        "question": "How do you assess completion of an AI model test?", "answer_outline": "Describe the checks you actually performed."}], "context_notes": []}, request)
    assert "completion" in result.entries[0].question
