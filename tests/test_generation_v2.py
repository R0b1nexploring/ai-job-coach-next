from copy import deepcopy
import json

import pytest

from app.content_v2 import Claim, FactSnapshot
from app.generation_v2 import CompatibleWriterV2, LocalWriterV2, generate_bundle_v2, generate_guide_v2, materialize_bundle_v2
from app.provider_contract_v2 import BundleCandidate


def snapshot():
    return FactSnapshot(snapshot_id="s", private_scope="local", claims=(
        Claim(claim_id="c1", fact_type="action", value="整理设备记录", confirmation="unconfirmed"),
        Claim(claim_id="c2", fact_type="school", value="虚构大学"),
    ))


def test_local_dual_documents_id_map_and_guides():
    provider = LocalWriterV2()
    result = generate_bundle_v2(provider, jd_text="虚构设备工程岗位", snapshot=snapshot(), private_header={}, resume_locale="zh-CN")
    assert set(result["documents"]) == {"job", "blueprint"}
    assert result["review_status"] == "not_run" and result["source"] == "local"
    job, blueprint = result["documents"]["job"], result["documents"]["blueprint"]
    assert {section["type"] for section in job["sections"]} == {"experience", "education"}
    project = next(s for s in blueprint["sections"] if s["type"] == "projects")["entries"][0]
    assert project["units"][0]["completion"] == "planned"
    assert job["sections"][0]["entries"][0]["units"][0]["completion"] == "unknown"
    restored = materialize_bundle_v2(BundleCandidate.model_validate(result["candidate"]), snapshot(), id_map=result["id_map"])
    assert restored["documents"] == result["documents"] and restored["id_map"] == result["id_map"]
    invalid = dict(result["id_map"])
    invalid.pop(next(key for key in invalid if key != "_candidate_hash"))
    with pytest.raises(ValueError, match="incomplete"):
        materialize_bundle_v2(BundleCandidate.model_validate(result["candidate"]), snapshot(), id_map=invalid)
    for doc in (job, blueprint):
        guide = generate_guide_v2(provider, document=doc, resume_id="resume", resume_revision=1, semantic_hash="f" * 64, jd_text="虚构设备工程岗位", private_header={}, guide_locale="en", fact_snapshot=snapshot(), source_types={}, plans=result["plans"])
        assert guide["locale"] == "en" and guide["kind"] == doc["kind"]
        assert guide["review_status"] == "not_run" and guide["coverage_unit_ids"]
        assert {ref for entry in guide["entries"] for ref in entry["unit_ids"]} == set(guide["coverage_unit_ids"])


def test_compatible_transport_only_sees_projected_facts_and_no_binding_header():
    class RecordingTransport:
        def __init__(self): self.calls = []
        def _call(self, system, user):
            payload = json.loads(user)
            self.calls.append(payload)
            if "resume_locale" in payload["input"]:
                from app.provider_contract_v2 import WriterRequest
                return LocalWriterV2().bundle(WriterRequest.model_validate(payload["input"]))
            from app.provider_contract_v2 import GuideRequest
            restored = {**payload["input"], "resume_id": "local", "resume_revision": 1, "semantic_hash": "f" * 64}
            return LocalWriterV2().guide(GuideRequest.model_validate(restored))
    transport = RecordingTransport()
    provider = CompatibleWriterV2(transport)
    sensitive = FactSnapshot(snapshot_id="s", private_scope="local", claims=(Claim(claim_id="c1", fact_type="action", value="整理设备记录"), Claim(claim_id="c2", fact_type="action", value="虚构私密姓名负责记录")))
    result = generate_bundle_v2(provider, jd_text="虚构岗位", snapshot=sensitive, private_header={"name": "虚构私密姓名", "email": "test@example.invalid"}, resume_locale="en")
    assert len(transport.calls[0]["input"]["facts"]) == 1
    guide = generate_guide_v2(provider, document=result["documents"]["job"], resume_id="local", resume_revision=1, semantic_hash="f" * 64, jd_text="虚构岗位", private_header={"name": "虚构私密姓名"}, guide_locale="en", fact_snapshot=sensitive, source_types={}, plans=result["plans"])
    assert guide["source"] == "compatible"
    text = json.dumps(transport.calls, ensure_ascii=False)
    assert "虚构私密姓名" not in text and "test@example.invalid" not in text
    assert "resume_id" not in transport.calls[-1]["input"] and "semantic_hash" not in transport.calls[-1]["input"]


def test_rejects_invalid_provider_output_and_contact_in_guide_before_network():
    class Bad(LocalWriterV2):
        def bundle(self, request):
            payload = super().bundle(request)
            payload["job"]["summary"]["claim_refs"] = ["unknown"]
            return payload
    with pytest.raises(ValueError):
        generate_bundle_v2(Bad(), jd_text="虚构岗位", snapshot=snapshot(), private_header={}, resume_locale="zh-CN")
    provider = LocalWriterV2()
    result = generate_bundle_v2(provider, jd_text="虚构岗位", snapshot=snapshot(), private_header={}, resume_locale="zh-CN")
    document = deepcopy(result["documents"]["job"])
    document["summary"]["text"] = "联系13800000000本人"
    with pytest.raises(ValueError):
        generate_guide_v2(provider, document=document, resume_id="r", resume_revision=1, semantic_hash="f" * 64, jd_text="虚构岗位", private_header={}, guide_locale="zh-CN", fact_snapshot=snapshot(), source_types={}, plans=result["plans"])


def test_provider_factory_requires_explicit_selection_and_frozen_configuration(monkeypatch):
    from app.generation_v2 import build_writer_v2
    monkeypatch.delenv("COACH_PROVIDER", raising=False)
    with pytest.raises(ValueError, match="explicitly"):
        build_writer_v2()
    monkeypatch.setenv("COACH_PROVIDER", "local")
    provider = build_writer_v2()
    assert build_writer_v2(provider.configuration).name == "local"
    with pytest.raises(ValueError, match="changed"):
        build_writer_v2({"provider": "compatible"})
    monkeypatch.setenv("COACH_PROVIDER", "compatible")
    monkeypatch.setenv("COACH_API_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("COACH_MODEL", "fictional-model")
    monkeypatch.setenv("COACH_API_KEY", "fictional-secret")
    configured = build_writer_v2()
    assert "fictional-secret" not in repr(configured.configuration)
    assert build_writer_v2(configured.configuration).name == "compatible"
    monkeypatch.setenv("COACH_MODEL", "other-model")
    with pytest.raises(ValueError, match="changed"):
        build_writer_v2(configured.configuration)


def test_review_independent_errors_block_unavailable_is_not_pass():
    from app.generation_v2 import review_bundle_v2
    provider = LocalWriterV2()
    result = generate_bundle_v2(provider, jd_text="虚构岗位", snapshot=snapshot(), private_header={}, resume_locale="zh-CN")
    args = dict(result=result, jd_text="虚构岗位", snapshot=snapshot(), private_header={}, resume_locale="zh-CN")
    assert review_bundle_v2(provider, **args)["status"] == "unavailable"
    target = result["documents"]["job"]["summary"]["unit_id"]
    class ReviewProvider(LocalWriterV2):
        name = "fictional"
        def __init__(self, response): self.response = response
        def review(self, payload):
            assert "private_header" not in payload and "candidate" not in payload
            return self.response
    issue = {"severity": "error", "scope": "job", "target_id": target, "diagnosis": "待核对的实质问题", "recommendation": "核对事实"}
    report = review_bundle_v2(ReviewProvider({"issues": [issue]}), **args)
    assert report["status"] == "blocked" and len(report["issues"]) == 1
    issue["severity"] = "suggestion"
    assert review_bundle_v2(ReviewProvider({"issues": [issue]}), **args)["status"] == "suggestions"
    issue["target_id"] = "unrelated"
    assert review_bundle_v2(ReviewProvider({"issues": [issue]}), **args)["status"] == "unavailable"
    assert review_bundle_v2(ReviewProvider({"issues": []}), **args)["status"] == "passed"


def test_saved_id_map_cannot_be_reused_for_a_different_candidate():
    result = generate_bundle_v2(LocalWriterV2(), jd_text="虚构岗位", snapshot=snapshot(), private_header={}, resume_locale="zh-CN")
    altered = deepcopy(result["candidate"])
    altered["job"]["summary"]["text"] = "候选已改变"
    with pytest.raises(ValueError, match="does not match"):
        materialize_bundle_v2(BundleCandidate.model_validate(altered), snapshot(), id_map=result["id_map"])


def test_provider_aliases_and_review_references_match_without_sending_private_id():
    from app.generation_v2 import review_bundle_v2
    class Capturing(LocalWriterV2):
        name = "fictional"
        def __init__(self): self.seen = []
        def bundle(self, request):
            self.seen.append(request.model_dump(mode="json"))
            return super().bundle(request)
        def review(self, payload):
            self.seen.append(deepcopy(payload))
            fact_id = payload["facts"][0]["claim_id"]
            entry = payload["documents"]["job"]["sections"][0]["entries"][0]["units"][0]
            assert entry["claim_refs"] == [fact_id]
            return {"issues": []}
    provider = Capturing()
    original = "probe@example.invalid"
    facts = FactSnapshot(snapshot_id="s", private_scope="local", claims=(Claim(claim_id=original, fact_type="action", value="整理设备记录"),))
    args = dict(jd_text="虚构工程岗位", snapshot=facts, private_header={"email": original}, resume_locale="zh-CN")
    result = generate_bundle_v2(provider, **args)
    assert result["documents"]["job"]["sections"][0]["entries"][0]["units"][0]["claim_refs"] == [original]
    assert review_bundle_v2(provider, result=result, **args)["status"] == "passed"
    assert original not in json.dumps(provider.seen, ensure_ascii=False)


def test_compatible_bundle_retries_one_fresh_request_without_echoing_invalid_output():
    from app.provider_contract_v2 import WriterRequest
    class CorrectingTransport:
        def __init__(self): self.requests = []
        def _call(self, system, user):
            payload = json.loads(user)
            self.requests.append((system, payload))
            request = WriterRequest.model_validate(payload["input"])
            output = LocalWriterV2().bundle(request)
            if len(self.requests) == 1:
                output["job"]["sections"][0]["entries"][0]["name"] = "private-output-probe@example.invalid"
            return output
    transport = CorrectingTransport()
    result = generate_bundle_v2(CompatibleWriterV2(transport), jd_text="虚构岗位", snapshot=snapshot(), private_header={}, resume_locale="en")
    assert set(result["documents"]) == {"job", "blueprint"}
    assert len(transport.requests) == 2
    assert "name to null" in transport.requests[0][0]
    assert "correction_instruction" in transport.requests[1][1]
    assert "private-output-probe@example.invalid" not in json.dumps(transport.requests[1], ensure_ascii=False)
    assert transport.requests[0][1]["input"] == transport.requests[1][1]["input"]


def test_compatible_bundle_stops_after_two_invalid_model_responses():
    from app.provider_contract_v2 import WriterRequest
    class InvalidTransport:
        def __init__(self): self.calls = 0
        def _call(self, system, user):
            self.calls += 1
            request = WriterRequest.model_validate(json.loads(user)["input"])
            output = LocalWriterV2().bundle(request)
            output["job"]["sections"][0]["entries"][0]["name"] = "unsupported"
            return output
    transport = InvalidTransport()
    with pytest.raises(ValueError, match="uncited entry name"):
        generate_bundle_v2(CompatibleWriterV2(transport), jd_text="虚构岗位", snapshot=snapshot(), private_header={}, resume_locale="en")
    assert transport.calls == 2


def test_guide_compatible_transport_aliases_short_header_and_restores_saved_unit_ids():
    from app.provider_contract_v2 import GuideRequest
    sensitive_id = "probe@example.invalid"
    facts = FactSnapshot(snapshot_id="s", private_scope="local", claims=(
        Claim(claim_id=sensitive_id, fact_type="action", value="整理设备记录", confirmation="user_confirmed"),
    ))
    generated = generate_bundle_v2(LocalWriterV2(), jd_text="虚构工程岗位", snapshot=facts,
                                   private_header={}, resume_locale="zh-CN")
    document = deepcopy(generated["documents"]["job"])
    document["summary"]["unit_id"] = sensitive_id
    document["summary"]["claim_refs"] = [sensitive_id]
    original_ids = [document["summary"]["unit_id"]] + [u["unit_id"] for s in document["sections"] for e in s["entries"] for u in e["units"]]
    class Transport:
        def __init__(self): self.requests = []
        def _call(self, system, user):
            payload = json.loads(user)["input"]
            self.requests.append(payload)
            local = {**payload, "resume_id": "local", "resume_revision": 1, "semantic_hash": "f" * 64}
            return LocalWriterV2().guide(GuideRequest.model_validate(local))
    transport = Transport()
    result = generate_guide_v2(CompatibleWriterV2(transport), document=document,
        resume_id="aaaaaaaaaaaa4aaa8aaaaaaaaaaaaaaa", resume_revision=1, semantic_hash="a" * 64,
        jd_text="虚构工程岗位", private_header={"name": "a", "email": sensitive_id}, guide_locale="zh-CN",
        fact_snapshot=facts, source_types={}, plans=generated["plans"])
    assert len(transport.requests) == 1
    serialized = json.dumps(transport.requests, ensure_ascii=False)
    assert sensitive_id not in serialized
    assert "resume_id" not in transport.requests[0] and "semantic_hash" not in transport.requests[0]
    assert all(identifier not in serialized for identifier in original_ids)
    assert {unit for entry in result["entries"] for unit in entry["unit_ids"]} == set(result["coverage_unit_ids"])
    assert sensitive_id in result["coverage_unit_ids"]


def test_guide_rejects_unknown_provider_unit_alias_before_saving():
    provider = LocalWriterV2()
    frozen = snapshot()
    result = generate_bundle_v2(provider, jd_text="虚构岗位", snapshot=frozen, private_header={}, resume_locale="zh-CN")
    class Forging(LocalWriterV2):
        def guide(self, request):
            output = super().guide(request)
            output["entries"][0]["unit_ids"] = ["forged-unit"]
            return output
    with pytest.raises(ValueError):
        generate_guide_v2(Forging(), document=result["documents"]["job"], resume_id="r", resume_revision=1,
            semantic_hash="f" * 64, jd_text="虚构岗位", private_header={}, guide_locale="zh-CN",
            fact_snapshot=frozen, source_types={}, plans=result["plans"])


def test_guide_plan_status_keys_are_not_treated_as_private_values():
    frozen = snapshot()
    generated = generate_bundle_v2(LocalWriterV2(), jd_text="虚构工程岗位", snapshot=frozen, private_header={}, resume_locale="zh-CN")
    plans = deepcopy(generated["plans"])
    plans[0]["needs_review"] = ["task", "actions"]
    plans[0]["field_authorship"] = {"task": "user_edited_unconfirmed", "actions": "derived_from_saved_units"}
    class Capture(LocalWriterV2):
        def guide(self, request):
            assert request.plans[0].needs_review == ("task", "actions")
            assert request.plans[0].field_authorship["task"] == "user_edited_unconfirmed"
            return super().guide(request)
    result = generate_guide_v2(Capture(), document=generated["documents"]["blueprint"], resume_id="r-a",
        resume_revision=1, semantic_hash="a" * 64, jd_text="虚构工程岗位", private_header={"name": "a"},
        guide_locale="zh-CN", fact_snapshot=frozen, source_types={}, plans=plans)
    assert result["coverage_unit_ids"]


def test_compatible_guide_corrects_internal_terms_once_without_echoing_model_text():
    from app.provider_contract_v2 import GuideRequest
    request = GuideRequest.model_validate({"resume_id": "r", "resume_revision": 1, "semantic_hash": "f" * 64,
        "kind": "job", "guide_locale": "zh-CN", "jd_text": "虚构岗位", "units": [{"unit_id": "u1", "text": "整理记录", "completion": "unknown"}]})
    class Transport:
        def __init__(self, always_invalid=False): self.inputs = []; self.always_invalid = always_invalid
        def _call(self, system, user):
            self.inputs.append(json.loads(user))
            result = LocalWriterV2().guide(request)
            if len(self.inputs) == 1 or self.always_invalid:
                result["entries"][0]["answer_outline"] = "单位文本标为 AI 改写 SECRET_OUTPUT_FROM_MODEL"
            return result
    transport = Transport()
    result = CompatibleWriterV2(transport).guide(request)
    assert len(transport.inputs) == 2 and result["entries"]
    assert transport.inputs[0]["input"] == transport.inputs[1]["input"]
    assert "SECRET_OUTPUT_FROM_MODEL" not in json.dumps(transport.inputs[1], ensure_ascii=False)
    assert "correction_instruction" in transport.inputs[1]
    failing = Transport(always_invalid=True)
    with pytest.raises(ValueError, match="internal metadata"):
        CompatibleWriterV2(failing).guide(request)
    assert len(failing.inputs) == 2


def test_blueprint_summary_purpose_structural_retry_never_rewrites_first_output():
    from app.provider_contract_v2 import WriterRequest
    class SummaryTransport:
        def __init__(self): self.requests, self.outputs = [], []
        def _call(self, system, user):
            payload = json.loads(user)
            self.requests.append((system, payload))
            request = WriterRequest.model_validate(payload["input"])
            output = LocalWriterV2().bundle(request)
            if len(self.requests) == 1:
                output["blueprint"]["summary"]["purpose"] = "proposal"
            self.outputs.append(deepcopy(output))
            return output
    transport = SummaryTransport()
    result = generate_bundle_v2(CompatibleWriterV2(transport), jd_text="虚构岗位", snapshot=snapshot(), private_header={}, resume_locale="en")
    assert transport.outputs[0]["blueprint"]["summary"]["purpose"] == "proposal"
    assert transport.outputs[1]["blueprint"]["summary"]["purpose"] == "summary"
    assert result["documents"]["blueprint"]["summary"]["purpose"] == "summary"
    assert len(transport.requests) == 2
    assert "summary.purpose must be exactly summary" in transport.requests[0][0]
    assert "purpose=summary" in transport.requests[1][1]["correction_instruction"]
    assert "blueprint" in transport.requests[1][1]["correction_instruction"]


@pytest.mark.parametrize(("locale", "expected"), [
    ("zh-CN", "请填写个人背景与求职目标。"),
    ("en", "Add your background and career goal."),
])
def test_no_model_facts_requires_exact_unclaimed_job_skeleton(locale, expected):
    from app.provider_contract_v2 import build_writer_request, validate_bundle_output
    empty = FactSnapshot(snapshot_id="s", private_scope="local", claims=())
    request = build_writer_request(jd_text="虚构岗位", snapshot=empty, private_header={}, resume_locale=locale)
    raw = LocalWriterV2().bundle(request)
    assert raw["job"]["summary"]["text"] == expected
    assert raw["job"]["summary"]["purpose"] == "summary" and raw["job"]["summary"].get("claim_refs", []) == []
    validate_bundle_output(raw, request)
    bad = deepcopy(raw);bad["job"]["summary"]["text"] = "虚构任职及资历"
    with pytest.raises(ValueError, match="fixed job summary"):
        validate_bundle_output(bad, request)
    result = generate_bundle_v2(LocalWriterV2(), jd_text="虚构岗位", snapshot=empty, private_header={}, resume_locale=locale)
    assert result["documents"]["job"]["summary"]["text"] == expected
    assert result["documents"]["job"]["summary"]["authorship"] == "ai_proposed"


def test_exact_empty_job_placeholder_generates_document_level_notes_without_invented_units():
    document = {"schema_version": 2, "kind": "job", "locale": "zh-CN", "title": "求职草稿", "summary": {
        "unit_id": "s1", "text": "请填写个人背景与求职目标。", "claim_refs": [], "support": [],
        "authorship": "ai_proposed", "completion": "unknown", "purpose": "summary", "plan_ref": None, "locked": False}, "sections": []}
    class CheckEmpty(LocalWriterV2):
        def guide(self, request):
            assert request.units == () and request.facts == () and request.plans == ()
            return {"kind": "job", "locale": "zh-CN", "entries": [], "context_notes": [
                {"text": "先整理真实课程和项目材料，再练习说明岗位检查思路。", "requirement_ids": []}]}
    result = generate_guide_v2(CheckEmpty(), document=document, resume_id="r1", resume_revision=1,
        semantic_hash="a" * 64, jd_text="虚构岗位需要资料整理和检查思路", private_header={}, guide_locale="zh-CN",
        fact_snapshot=FactSnapshot(snapshot_id="s", private_scope="local", claims=()), source_types={}, plans=[])
    assert result["coverage_unit_ids"] == [] and result["entries"] == []
    assert result["context_notes"] and result["context_notes"][0]["text"]


def test_blueprint_retains_available_work_and_education_but_not_forced_disputed():
    from app.provider_contract_v2 import build_writer_request, validate_bundle_output
    frozen = FactSnapshot(snapshot_id="s", private_scope="local", claims=(
        Claim(claim_id="employer", fact_type="employer", value="虚构公司"),
        Claim(claim_id="school", fact_type="school", value="虚构学校"),
    ))
    request = build_writer_request(jd_text="虚构岗位", snapshot=frozen, private_header={}, resume_locale="zh-CN")
    raw = LocalWriterV2().bundle(request)
    for kind in ("job", "blueprint"):
        sections = raw[kind]["sections"]
        assert any(s["type"] == "experience" and any(e.get("organization_ref") for e in s["entries"]) for s in sections)
        assert any(s["type"] == "education" and any(e.get("school_ref") for e in s["entries"]) for s in sections)
    validate_bundle_output(raw, request)
    bad = deepcopy(raw);bad["blueprint"]["sections"] = [s for s in bad["blueprint"]["sections"] if s["type"] != "experience"]
    with pytest.raises(ValueError, match="work background"):validate_bundle_output(bad, request)
    bad = deepcopy(raw);bad["blueprint"]["sections"] = [s for s in bad["blueprint"]["sections"] if s["type"] != "education"]
    with pytest.raises(ValueError, match="education background"):validate_bundle_output(bad, request)
    disputed = FactSnapshot(snapshot_id="s", private_scope="local", claims=tuple(c.model_copy(update={"confirmation":"disputed"}) for c in frozen.claims))
    dispute_request = build_writer_request(jd_text="虚构岗位", snapshot=disputed, private_header={}, resume_locale="zh-CN")
    disputed_output = LocalWriterV2().bundle(dispute_request)
    assert not any(s["type"] in {"experience", "education"} for s in disputed_output["blueprint"]["sections"])
    validate_bundle_output(disputed_output, dispute_request)


@pytest.mark.parametrize("section_type", ["experience", "education", "skills", "projects"])
def test_no_fact_job_may_contain_empty_entries_but_no_narrative_units(section_type):
    from app.provider_contract_v2 import build_writer_request, validate_bundle_output
    frozen = FactSnapshot(snapshot_id="s", private_scope="local", claims=())
    request = build_writer_request(jd_text="虚构岗位", snapshot=frozen, private_header={}, resume_locale="en")
    raw = LocalWriterV2().bundle(request)
    entry = {"temp_key":"empty-entry", "units": []}
    if section_type == "projects":entry["plan_ref"] = "practice-plan"
    raw["job"]["sections"] = [{"temp_key":"empty-section", "type":section_type, "entries":[entry]}]
    validate_bundle_output(raw,request)
    entry["units"] = [{"temp_key":"invented-unit", "text":"Fictional prior work", "purpose":"experience"}]
    with pytest.raises(ValueError,match="empty facts"):
        validate_bundle_output(raw,request)
    entry["units"] = []
    for field, value in (("name", "虚构项目"), ("category", "虚构技能"), ("role_ref", "unknown")):
        bad = deepcopy(raw)
        bad["job"]["sections"][0]["entries"][0][field] = value
        with pytest.raises(ValueError,match="empty facts"):
            validate_bundle_output(bad,request)
