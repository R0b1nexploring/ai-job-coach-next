"""Bounded external-service failures using fictional data and HTTP stubs."""
import json

import httpx
import pytest

from app.providers import CompatibleProvider, FallbackProvider, LocalProvider, build_provider

JD = "虚构岗位：机械设计工程师，负责装配结构设计与工程图输出。"


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
def test_bad_response_is_explicit_local_fallback(compatible, monkeypatch, payload):
    calls = []
    monkeypatch.setattr("app.providers.httpx.post", lambda *a, **k: calls.append(1) or reply(200, payload))
    provider = FallbackProvider(compatible)
    provider.bundle(JD, "")
    assert provider.last_source == "degraded_local"
    assert len(calls) == 1


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
