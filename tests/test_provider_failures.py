"""Bounded external-service failures using fictional data and HTTP stubs."""
import json
from copy import deepcopy
from pathlib import Path
import subprocess

import httpx
import pytest

from app.providers import CompatibleProvider, FallbackProvider, LocalProvider, build_provider

JD = "虚构岗位：机械设计工程师，负责装配结构设计与工程图输出。"
DIAGNOSTIC_FILTER = Path(__file__).parents[1] / "scripts" / "provider-diagnostic-filter.awk"


@pytest.fixture
def compatible(monkeypatch):
    monkeypatch.setenv("COACH_API_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("COACH_MODEL", "fictional-model")
    monkeypatch.setenv("COACH_API_KEY", "")
    monkeypatch.setattr("app.providers.time.sleep", lambda _: None)
    return CompatibleProvider()


def reply(status, payload):
    return httpx.Response(status, json=payload, request=httpx.Request("POST", "https://example.invalid/v1/chat/completions"))


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
def test_http_failures_have_bounded_retries(compatible, monkeypatch, status):
    calls = []
    monkeypatch.setattr("app.providers.httpx.post", lambda *a, **k: calls.append(1) or reply(status, {"error": "fictional failure"}))
    provider = FallbackProvider(compatible)
    result = provider.bundle(JD, "")
    assert result["job"]["items"]
    assert provider.last_source == "degraded_local"
    assert len(calls) == 3


def test_success_after_transient_error(compatible, monkeypatch):
    raw = LocalProvider().bundle(JD, "")
    responses = iter([reply(429, {}), reply(200, {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(raw)}}]})])
    monkeypatch.setattr("app.providers.httpx.post", lambda *a, **k: next(responses))
    provider = FallbackProvider(compatible)
    assert provider.bundle(JD, "") == raw
    assert provider.last_source == "compatible"


@pytest.mark.parametrize("payload", [{}, {"choices": []}, {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}, {"choices": [{"message": {"content": ""}}]}, {"choices": [{"message": {"content": "broken json"}}]}])
def test_bad_response_is_explicit_local_fallback(compatible, monkeypatch, payload, caplog):
    calls = []
    monkeypatch.setattr("app.providers.httpx.post", lambda *a, **k: calls.append(1) or reply(200, payload))
    provider = FallbackProvider(compatible)
    provider.bundle(JD, "")
    assert provider.last_source == "degraded_local"
    assert len(calls) == 1
    assert "provider fallback operation=bundle category=" in caplog.text
    filtered = subprocess.run(["awk", "-f", str(DIAGNOSTIC_FILTER)], input=caplog.text, text=True, capture_output=True, check=True).stdout
    assert "provider_error operation=bundle category=" in filtered
    assert "fictional failure" not in filtered


def test_successful_bai_chat_completion_contract_and_json_mode(compatible, monkeypatch):
    raw = LocalProvider().bundle(JD, "")
    calls = []
    def post(url, **kwargs):
        calls.append((url, kwargs))
        return reply(200, {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(raw, ensure_ascii=False)}}]})
    monkeypatch.setattr("app.providers.httpx.post", post)
    provider = FallbackProvider(compatible)
    assert provider.bundle(JD, "") == raw
    assert provider.last_source == "compatible"
    assert calls[0][0].endswith("/chat/completions")
    assert calls[0][1]["json"]["response_format"] == {"type": "json_object"}
    assert "stream" not in calls[0][1]["json"]  # B.AI documents non-streaming as the default
    assert calls[0][1]["json"]["messages"][0]["role"] == "system"
    assert calls[0][1]["json"]["messages"][1]["role"] == "user"


def test_quality_invalid_model_response_corrects_then_logs_fallback(compatible, monkeypatch, caplog):
    raw = LocalProvider().bundle(JD, "")
    raw["job"]["items"][0]["plan_key"] = "not-a-real-plan"
    outputs = iter([raw, raw])
    def post(_url, **kwargs):
        return reply(200, {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(next(outputs), ensure_ascii=False)}}]})
    monkeypatch.setattr("app.providers.httpx.post", post)
    provider = FallbackProvider(compatible)
    result = provider.bundle(JD, "")
    assert provider.last_source == "degraded_local"
    assert result["job"]["items"]
    assert "category=response_validation" in caplog.text
    filtered = subprocess.run(["awk", "-f", str(DIAGNOSTIC_FILTER)], input=caplog.text, text=True, capture_output=True, check=True).stdout
    assert "provider_error operation=bundle category=response_validation" in filtered
    assert "not-a-real-plan" not in filtered


def test_short_item_retries_with_explicit_length_constraint(compatible, monkeypatch):
    valid = LocalProvider().bundle(JD, "")
    invalid = deepcopy(valid)
    invalid["job"]["items"][0]["text"] = "过短"
    outputs = iter([invalid, valid])
    calls = []
    def post(_url, **kwargs):
        calls.append(kwargs["json"])
        return reply(200, {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(next(outputs), ensure_ascii=False)}}]})
    monkeypatch.setattr("app.providers.httpx.post", post)
    assert compatible.bundle(JD, "") == valid
    assert len(calls) == 2
    assert "12 至 600" in calls[0]["messages"][0]["content"]
    assert "12 至 600" in calls[1]["messages"][1]["content"]


def test_timeout_has_finite_local_fallback(compatible, monkeypatch):
    def timeout(*_args, **_kwargs):
        raise httpx.ReadTimeout("fictional timeout")
    monkeypatch.setattr("app.providers.httpx.post", timeout)
    provider = FallbackProvider(compatible)
    provider.bundle(JD, "")
    assert provider.last_source == "degraded_local"


def test_unsafe_or_unknown_configuration_rejected(monkeypatch):
    monkeypatch.setenv("COACH_PROVIDER", "typo")
    with pytest.raises(ValueError):
        build_provider()
    monkeypatch.setenv("COACH_MODEL", "fictional-model")
    for endpoint in ("http://localhost.attacker.invalid/v1", "https://secret@example.invalid/v1", "https://example.invalid/v1?key=secret"):
        monkeypatch.setenv("COACH_API_BASE_URL", endpoint)
        with pytest.raises(ValueError):
            CompatibleProvider()
