"""Review transport boundaries with fictional material and stubbed HTTP only."""
from copy import deepcopy
import json

import httpx
import pytest

from app.content import normalize_bundle
from app.providers import (
    BUNDLE_SYSTEM,
    GUIDE_SYSTEM,
    REPAIR_SYSTEM,
    REVIEW_SYSTEM,
    CompatibleProvider,
    FallbackProvider,
    LocalProvider,
)


JD = "虚构岗位：设计装配结构并输出工程图。"
MATERIAL = "课程项目中整理了装配图纸，负责尺寸检查。"


@pytest.fixture
def compatible(monkeypatch):
    monkeypatch.setenv("COACH_API_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("COACH_MODEL", "fictional-review-model")
    monkeypatch.delenv("COACH_REASONING_EFFORT", raising=False)
    monkeypatch.setenv("COACH_API_KEY", "")
    monkeypatch.setenv("COACH_TIMEOUT_SECONDS", "30")
    monkeypatch.setattr("app.providers.time.sleep", lambda _: None)
    return CompatibleProvider()


def review_payload():
    return {
        "jd": JD,
        "material": MATERIAL,
        "material_confirmed": False,
        "draft": normalize_bundle(LocalProvider().bundle(JD, MATERIAL), JD, MATERIAL, False),
    }


def response(content, *, status=200, finish_reason="stop"):
    return httpx.Response(
        status,
        json={"choices": [{"finish_reason": finish_reason, "message": {"content": content}}]},
        request=httpx.Request("POST", "https://example.invalid/v1/chat/completions"),
    )


def test_review_repair_and_reassessment_have_independent_contexts(compatible, monkeypatch):
    payload = review_payload()
    original = deepcopy(payload)
    text = payload["draft"]["resumes"]["job"]["items"][0]["text"]
    assessment = {
        "assessed_scopes": ["job", "blueprint", "plans"],
        "issues": [{
            "id": "issue-1", "severity": "error", "code": "contribution_boundary",
            "targets": [{"scope": "job", "target_id": "job-1", "field": "text", "quote": text}],
            "diagnosis": "没有区分整理图纸与设计整个装配的贡献范围。",
            "instruction": "保留图纸整理与尺寸检查的本人行动。", "jd_quote": "设计装配结构",
        }],
    }
    repaired_text = "在课程项目中整理装配图纸，核对尺寸并记录需要进一步确认的问题。"
    repairs = {"edits": [{
        "issue_id": "issue-1", "scope": "job", "target_id": "job-1", "field": "text",
        "before": text, "after": repaired_text,
    }]}
    clean = {"assessed_scopes": ["job", "blueprint", "plans"], "issues": []}
    outputs = iter([assessment, repairs, clean])
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return response(json.dumps(next(outputs), ensure_ascii=False))

    monkeypatch.setattr("app.providers.httpx.post", post)
    assert compatible.review(payload) == assessment
    repair_input = {**deepcopy(payload), "issues": assessment["issues"]}
    assert compatible.repair(repair_input) == repairs
    final_input = deepcopy(payload)
    final_input["draft"]["resumes"]["job"]["items"][0]["text"] = repaired_text
    assert compatible.review(final_input) == clean

    assert payload == original
    assert len(calls) == 3
    assert len({REVIEW_SYSTEM, REPAIR_SYSTEM, BUNDLE_SYSTEM, GUIDE_SYSTEM}) == 4
    for (url, request), system, expected in zip(
        calls, [REVIEW_SYSTEM, REPAIR_SYSTEM, REVIEW_SYSTEM], [payload, repair_input, final_input], strict=True,
    ):
        assert url == "https://example.invalid/v1/chat/completions"
        assert request["headers"] == {}
        assert request["timeout"] == 30
        body = request["json"]
        assert body["model"] == "fictional-review-model"
        assert body["temperature"] == 0.3
        assert "reasoning_effort" not in body
        assert body["response_format"] == {"type": "json_object"}
        assert body["messages"] == [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(expected, ensure_ascii=False)},
        ]
        assert json.loads(body["messages"][1]["content"]) == expected
        assert '"header"' not in body["messages"][1]["content"]
    assert "issues" not in json.loads(calls[2][1]["json"]["messages"][1]["content"])


def test_configured_reasoning_effort_replaces_temperature(monkeypatch):
    monkeypatch.setenv("COACH_API_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("COACH_MODEL", "fictional-reasoning-model")
    monkeypatch.setenv("COACH_REASONING_EFFORT", "high")
    calls = []
    def post(_url, **kwargs):
        calls.append(kwargs["json"])
        return response('{}')
    monkeypatch.setattr("app.providers.httpx.post", post)
    assert CompatibleProvider()._call("system", "user") == {}
    assert calls[0]["reasoning_effort"] == "high"
    assert "temperature" not in calls[0]


def test_invalid_reasoning_effort_rejected(monkeypatch):
    monkeypatch.setenv("COACH_API_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("COACH_MODEL", "fictional-model")
    monkeypatch.setenv("COACH_REASONING_EFFORT", "invalid")
    with pytest.raises(ValueError, match="reasoning effort"):
        CompatibleProvider()


@pytest.mark.parametrize("operation", ["review", "repair"])
def test_invalid_json_is_not_retried_as_content_correction(compatible, monkeypatch, operation):
    calls = []

    def post(_url, **kwargs):
        calls.append(kwargs)
        return response("not valid JSON")

    monkeypatch.setattr("app.providers.httpx.post", post)
    with pytest.raises(json.JSONDecodeError):
        getattr(compatible, operation)(review_payload())
    assert len(calls) == 1


@pytest.mark.parametrize("operation", ["review", "repair"])
def test_semantic_validation_stays_with_caller_without_hidden_regeneration(compatible, monkeypatch, operation):
    calls = []
    wrong_contract = {"unexpected": "fictional response requiring caller validation"}

    def post(_url, **kwargs):
        calls.append(kwargs)
        return response(json.dumps(wrong_contract))

    monkeypatch.setattr("app.providers.httpx.post", post)
    assert getattr(compatible, operation)(review_payload()) == wrong_contract
    assert len(calls) == 1


class NoSemanticFallback(LocalProvider):
    def review(self, payload):
        pytest.fail("a failed review must not become local approval")

    def repair(self, payload):
        pytest.fail("a failed repair must not become local success")


@pytest.mark.parametrize("operation, result", [
    ("review", {"assessed_scopes": ["job", "blueprint", "plans"], "issues": []}),
    ("repair", {"edits": []}),
])
@pytest.mark.parametrize("source", ["compatible", "degraded_local"])
def test_semantic_success_does_not_change_draft_source(compatible, monkeypatch, operation, result, source):
    monkeypatch.setattr("app.providers.httpx.post", lambda *args, **kwargs: response(json.dumps(result)))
    provider = FallbackProvider(compatible, NoSemanticFallback())
    provider.last_source = source
    assert getattr(provider, operation)(review_payload()) == result
    assert provider.last_source == source


@pytest.mark.parametrize("operation", ["review", "repair"])
@pytest.mark.parametrize("failure", ["timeout", "http_503", "truncated", "invalid_json"])
def test_semantic_failure_propagates_without_fallback_or_source_change(compatible, monkeypatch, operation, failure):
    calls = []

    def post(_url, **kwargs):
        calls.append(kwargs)
        if failure == "timeout":
            raise httpx.ReadTimeout("fictional timeout")
        if failure == "http_503":
            return response("{}", status=503)
        if failure == "truncated":
            return response("{}", finish_reason="length")
        return response("invalid json")

    monkeypatch.setattr("app.providers.httpx.post", post)
    provider = FallbackProvider(compatible, NoSemanticFallback())
    provider.last_source = "degraded_local"
    expected_error = {
        "timeout": httpx.ReadTimeout,
        "http_503": httpx.HTTPStatusError,
        "truncated": ValueError,
        "invalid_json": json.JSONDecodeError,
    }[failure]
    with pytest.raises(expected_error):
        getattr(provider, operation)(review_payload())
    assert provider.last_source == "degraded_local"
    # Existing transport retry policy remains bounded; no content repair loop.
    assert len(calls) == (3 if failure == "http_503" else 1)


def test_local_review_is_unavailable_and_cannot_repair():
    local = LocalProvider()
    assert local.review(review_payload()) is None
    with pytest.raises(ValueError, match="does not support semantic repair"):
        local.repair(review_payload())
    wrapped = FallbackProvider(local, NoSemanticFallback())
    assert wrapped.review(review_payload()) is None
    assert wrapped.last_source == "local"
    with pytest.raises(ValueError, match="does not support semantic repair"):
        wrapped.repair(review_payload())
    assert wrapped.last_source == "local"
