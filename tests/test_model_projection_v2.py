import json

from app.content_v2 import Claim, ClaimEvidence, FactSnapshot, text_hash
from app.model_projection_v2 import project_facts_for_model


def fact(identifier, kind, value, **kwargs):
    return Claim(claim_id=identifier, fact_type=kind, value=value, **kwargs)


def test_projection_only_contains_allowlisted_facts_and_no_private_probes():
    quote = "虚构姓名：林探针，邮箱：probe@example.invalid，参加设备整理"
    evidence = ClaimEvidence(source_revision_id="source-private", start=0, end=len(quote), quote_hash=text_hash(quote))
    snapshot = FactSnapshot(snapshot_id="private-snapshot", private_scope="private-scope", claims=(
        fact("safe", "action", "整理设备记录", evidence=(evidence,)),
        fact("name", "name", "林探针"),
        fact("unknown", "raw_material", quote),
        fact("embedded-name", "action", "林探针参与了设备整理"),
        fact("embedded-mail", "action", "请联系 probe@example.invalid 获取记录"),
        fact("embedded-phone", "action", "电话 +86 138 0000 0000"),
        fact("embedded-url", "work_sample", "https://example.invalid/profile"),
    ))
    output = project_facts_for_model(snapshot, private_header={"name": "林探针", "email": "probe@example.invalid", "phone": "13800000000", "education": "隐私教育探针"})
    assert [entry["claim_id"] for entry in output["claims"]] == ["safe"]
    serialized = json.dumps(output, ensure_ascii=False)
    for probe in ("林探针", "probe@example.invalid", "138", "example.invalid", "source-private", "private-scope", "private-snapshot", "隐私教育探针", text_hash(quote)):
        assert probe not in serialized
    assert "evidence" not in serialized and "header" not in serialized


def test_unknown_contact_and_extra_identity_values_are_rejected_without_header():
    snapshot = FactSnapshot(snapshot_id="s", private_scope="local", claims=(
        fact("mail", "action", "probe2@example.invalid"),
        fact("phone", "action", "联系 13800000000"),
        fact("private", "action", "来自敏感身份探针的记录"),
        fact("ok", "skill", "机械制图"),
    ))
    result = project_facts_for_model(snapshot, private_header={}, additional_private_values=("敏感身份探针",))
    assert [entry["claim_id"] for entry in result["claims"]] == ["ok"]


def test_omitted_claim_conflict_ids_do_not_leak_and_states_are_preserved():
    snapshot = FactSnapshot(snapshot_id="s", private_scope="local", claims=(
        fact("c1", "job_title", "工程师", confirmation="disputed", completion="unknown", conflict_ids=("c2",)),
        fact("c2", "name", "虚构姓名", conflict_ids=("c1",)),
    ))
    result = project_facts_for_model(snapshot, private_header={})
    assert result == {"claims": [{"claim_id": "c1", "fact_type": "job_title", "value": "工程师", "confirmation": "disputed", "completion": "unknown", "conflict_ids": []}]}


def test_no_free_text_fallback_and_limits_long_claims():
    snapshot = FactSnapshot(snapshot_id="s", private_scope="local", claims=(
        fact("other", "unclassified", "任意原文"),
        fact("long", "action", "长" * 501),
        fact("empty", "action", " "),
        fact("none", "action", None),
        fact("probe@example.invalid", "action", "整理记录"),
    ))
    assert project_facts_for_model(snapshot, private_header={}) == {"claims": []}


def test_phone_probes_adjacent_to_chinese_and_in_fullwidth_or_separated_form():
    values = (
        "我的电话13800000000请联系",
        "联系１３８００００００００本人",
        "电话＋８６（１３８）００００－００００可联系",
        "手机138/0000/0000可用",
        "联系138—0000—0000后沟通",
    )
    claims = tuple(fact(f"c{index}", "action", value) for index, value in enumerate(values))
    snapshot = FactSnapshot(snapshot_id="s", private_scope="local", claims=claims)
    assert project_facts_for_model(snapshot, private_header={}) == {"claims": []}


def test_diagnostics_are_separate_redacted_and_categorize_omissions():
    from app.model_projection_v2 import project_facts_with_diagnostics

    snapshot = FactSnapshot(snapshot_id="s", private_scope="local", claims=(
        fact("accepted", "action", "整理设备记录"),
        fact("wrong-type", "raw_material", "不发送的原文"),
        fact("private-value", "action", "林探针负责整理"),
        fact("empty", "action", "  "),
        fact("missing", "action", None),
        fact("too-long", "action", "长" * 501),
        fact("probe@example.invalid", "unclassified", "标记"),
    ))
    result = project_facts_with_diagnostics(snapshot, private_header={"name": "林探针"})
    assert result.model_input == project_facts_for_model(snapshot, private_header={"name": "林探针"})
    assert [item["claim_id"] for item in result.model_input["claims"]] == ["accepted"]
    assert [(entry["claim_id"], entry["reason"]) for entry in result.diagnostics.excluded] == [
        ("wrong-type", "unsupported_type"), ("private-value", "sensitive_value"),
        ("empty", "empty_value"), ("missing", "empty_value"),
        ("too-long", "value_too_long"), (None, "sensitive_identifier"),
    ]
    assert result.diagnostics.excluded[-1]["ordinal"] == 6
    diagnostic_text = repr(result.diagnostics)
    payload_text = repr(result.model_input)
    for probe in ("林探针", "不发送的原文", "probe@example.invalid", "长" * 501):
        assert probe not in diagnostic_text and probe not in payload_text
    assert "diagnostics" not in result.model_input and "excluded" not in result.model_input


def test_uuid4_ids_do_not_trigger_phone_detector_but_private_identifiers_do():
    from app.model_projection_v2 import contains_sensitive_identifier

    app_id = "123456789abc4def8123456789012345"
    assert not contains_sensitive_identifier(app_id, ())
    assert contains_sensitive_identifier(app_id, ("1234567",))
    # Invalid version, variant, and noncanonical case receive ordinary checks.
    for identifier in ("123456789abc3def8123456789012345", "123456789abc4def7123456789012345", app_id.upper(), "电话13800000000"):
        assert contains_sensitive_identifier(identifier, ())
    snapshot = FactSnapshot(snapshot_id="s", private_scope="local", claims=(fact(app_id, "action", "整理设备记录"),))
    assert project_facts_for_model(snapshot, private_header={})["claims"][0]["claim_id"] == app_id
    assert project_facts_for_model(snapshot, private_header={"name": "1234567"})["claims"] == []


def test_model_alias_does_not_require_any_fixed_prefix():
    from app.model_projection_v2 import project_facts_with_diagnostics
    snapshot = FactSnapshot(snapshot_id="s", private_scope="local", claims=(fact("c1", "action", "整理设备记录"),))
    result = project_facts_with_diagnostics(snapshot, private_header={"name": "q"}, alias_identifiers=True)
    assert len(result.model_input["claims"]) == 1
    assert "q" not in result.model_input["claims"][0]["claim_id"]
