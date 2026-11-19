from copy import deepcopy

import pytest

from app.guide_v2 import content_hash, coverage_units, guide_input_hash, semantic_hash, validate_guide_coverage


def _doc():
    return {"schema_version": 2, "kind": "job", "locale": "en", "title": "Engineer",
            "summary": {"unit_id": "s", "text": "Organized records", "claim_refs": ["c1"], "locked": False},
            "sections": [{"type": "experience", "section_id": "exp", "entries": [
                {"entry_id": "e", "units": [{"unit_id": "u1", "text": "Organized records",
                                               "claim_refs": ["c1"], "completion": "completed", "locked": False}]}]},
                {"type": "skills", "section_id": "skills", "entries": [
                    {"entry_id": "e2", "units": [{"unit_id": "u2", "text": "Uses CAD",
                                                     "claim_refs": ["c2"], "completion": "unknown"}]}]}]}


def test_semantic_hash_changes_for_claims_order_and_language_but_not_lock_or_header():
    document = _doc()
    facts = {"claims": [{"id": "c1", "confirmation": "unconfirmed"}]}
    base = semantic_hash(document, {"jd": "Engineer"}, facts, [])
    changed = deepcopy(document)
    changed["sections"][0]["entries"][0]["units"][0]["locked"] = True
    assert semantic_hash(changed, {"jd": "Engineer"}, facts, []) == base
    assert content_hash(document, {"email": "a@example.test"}, []) != content_hash(document, {"email": "b@example.test"}, [])
    facts["claims"][0]["confirmation"] = "user_confirmed"
    assert semantic_hash(document, {"jd": "Engineer"}, facts, []) != base
    changed = deepcopy(document)
    changed["sections"].reverse()
    assert semantic_hash(changed, {"jd": "Engineer"}, {"claims": [{"id": "c1", "confirmation": "unconfirmed"}]}, []) != base
    assert guide_input_hash(base, "en", ["u1"], "v1") != guide_input_hash(base, "zh-CN", ["u1"], "v1")


def test_coverage_tracks_units_and_rejects_invalid_guides():
    # Sharing a fact does not by itself prove the summary says the same thing.
    assert coverage_units(_doc()) == ["u1", "s", "u2"]
    entry = {"unit_ids": ["u1", "u2"], "question": "How?", "answer_outline": "Explain the work."}
    validate_guide_coverage([entry], [], ["u1", "u2"])
    with pytest.raises(ValueError):
        validate_guide_coverage([entry], [], ["u1"])
    with pytest.raises(ValueError):
        validate_guide_coverage([entry, entry], [], ["u1", "u2"])
    with pytest.raises(ValueError):
        validate_guide_coverage([], [], [])
    validate_guide_coverage([], [{"text": "Prepare an example using the job requirements."}], [])


def test_only_exact_unreferenced_ai_job_placeholder_is_excluded():
    from copy import deepcopy

    document = {"kind": "job", "locale": "zh-CN", "summary": {"unit_id": "s", "text": "请填写个人背景与求职目标。", "purpose": "summary", "authorship": "ai_proposed", "claim_refs": []}, "sections": []}
    assert coverage_units(document) == []
    variants = []
    edited = deepcopy(document); edited["summary"]["authorship"] = "user_edited"; variants.append(edited)
    human = deepcopy(document); human["summary"]["authorship"] = "user_text"; variants.append(human)
    cited = deepcopy(document); cited["summary"]["claim_refs"] = ["fact-1"]; variants.append(cited)
    real = deepcopy(document); real["summary"]["text"] = "能使用 AI 协助整理设备点检记录。"; variants.append(real)
    changed = deepcopy(document); changed["summary"]["text"] += " 本人有设备维护经历。"; variants.append(changed)
    blueprint = deepcopy(document); blueprint["kind"] = "blueprint"; variants.append(blueprint)
    wrong_locale = deepcopy(document); wrong_locale["locale"] = "en"; variants.append(wrong_locale)
    assert all(coverage_units(value) == ["s"] for value in variants)
    en = deepcopy(document); en["locale"] = "en"; en["summary"]["text"] = "Add your background and career goal."
    assert coverage_units(en) == []
    # With no personal units the guide must use document-level preparation,
    # without inventing an interview entry or unit reference.
    validate_guide_coverage([], [{"text": "整理现有课程材料，准备解释岗位检查思路。"}], [])
    with pytest.raises(ValueError, match="context notes"):
        validate_guide_coverage([], [], [])
