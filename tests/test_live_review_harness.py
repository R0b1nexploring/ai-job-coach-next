"""Offline subprocess checks for the isolated real-provider evaluation harness."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def run_isolated(code: str, output: Path) -> subprocess.CompletedProcess:
    # No developer provider credentials or database settings enter the child.
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("COACH_", "LLM_"))}
    env.update(COACH_API_BASE_URL="https://synthetic.invalid/v1", COACH_API_KEY="fictional-key",
               COACH_MODEL="fictional-offline-model", COACH_DATA_DIR=str(output),
               COACH_JOURNAL_DIR=str(output / "journal"),
               COACH_DATABASE_URL="sqlite:///" + str(output / "evaluation.sqlite"))
    return subprocess.run([sys.executable, "-c", code, str(output)], cwd=ROOT, env=env,
                          capture_output=True, text=True, timeout=30, check=False)


def test_normal_service_pipeline_and_evidence_are_self_consistent(tmp_path):
    output = tmp_path / "new-evaluation"
    result = run_isolated('''
import json
from pathlib import Path
import sys
import httpx
from app.providers import CompatibleProvider, LocalProvider, BUNDLE_SYSTEM, GUIDE_SYSTEM, REVIEW_SYSTEM
from scripts.evaluate_content_review import evaluate, JD, MATERIAL

def forbid_network(*args, **kwargs):
    raise AssertionError("offline test attempted network")
httpx.post = forbid_network

def fictional_call(self, system, user):
    local = LocalProvider()
    if system == BUNDLE_SYSTEM:
        return local.bundle(JD, MATERIAL)
    payload = json.loads(user)
    if system == REVIEW_SYSTEM:
        assert payload["material_confirmed"] is True
        assert set(payload["draft"]["resumes"]) == {"job", "blueprint"}
        return {"assessed_scopes": ["job", "blueprint", "plans"], "issues": []}
    if system == GUIDE_SYSTEM:
        return local.guide(payload["kind"], payload["resume"], payload["plans"], payload["jd"])
    raise AssertionError("unexpected model stage")
CompatibleProvider._call = fictional_call
output = Path(sys.argv[1])
output.mkdir()
assert evaluate(output) is True
''', output)
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads((output / "summary.json").read_text())
    evidence = json.loads((output / "evidence.json").read_text())
    assert summary["status"] == "passed"
    assert summary["checks"] and all(summary["checks"].values())
    assert summary["call_count"] == 4
    assert summary["call_error_count"] == 0
    assert summary["repair_exercised"] is False
    assert summary["review_status"] == "passed"
    assert [call["stage"] for call in evidence["calls"]] == ["bundle", "review", "guide", "guide"]
    assert evidence["before_review_draft"]["resumes"]
    assert len(evidence["immutable_inputs"]) == 3
    assert all(row["status"] == "succeeded" for row in evidence["executions"])
    assert (output / "evaluation.sqlite").is_file()


def test_nonempty_output_rejects_before_provider_or_network(tmp_path):
    output = tmp_path / "protected"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("synthetic existing output")
    result = run_isolated('''
from pathlib import Path
import sys
import httpx
from scripts import evaluate_content_review as harness

def forbidden(*args, **kwargs):
    raise AssertionError("protected output reached a provider or network")
harness.TrackingProvider = forbidden
httpx.post = forbidden
sys.argv = ["evaluate_content_review", "--output", sys.argv[1]]
assert harness.main() == 2
''', output)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["reason"] == "output_must_be_new_or_empty_directory"
    assert marker.read_text() == "synthetic existing output"
    assert list(output.iterdir()) == [marker]


def test_error_summary_excludes_request_response_and_exception_secrets(tmp_path):
    result = run_isolated('''
import json
import httpx
from scripts.evaluate_content_review import safe_error
secret = "fictional-secret-never-print"
request = httpx.Request("POST", "https://synthetic.invalid/" + secret,
                        headers={"Authorization": "Bearer " + secret})
response = httpx.Response(503, text=secret, request=request)
error = httpx.HTTPStatusError(secret, request=request, response=response)
value = safe_error(error)
assert value == {"category": "HTTPStatusError", "http_status": 503}
assert secret not in json.dumps(value)
assert secret not in json.dumps(safe_error(ValueError(secret)))
print(json.dumps(value))
''', tmp_path / "unused")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "fictional-secret-never-print" not in result.stdout + result.stderr


def test_controlled_fixture_uses_real_pipeline_repair_and_two_guides(tmp_path):
    output = tmp_path / "controlled-evaluation"
    result = run_isolated('''
import json
from pathlib import Path
import sys
import httpx
from app.providers import CompatibleProvider, LocalProvider, GUIDE_SYSTEM, REVIEW_SYSTEM, REPAIR_SYSTEM
from scripts.evaluate_content_review import evaluate

def forbidden(*args, **kwargs):
    raise AssertionError("offline test attempted network")
httpx.post = forbidden
reviews = []

def fictional_call(self, system, user):
    payload = json.loads(user)
    if system == REVIEW_SYSTEM:
        reviews.append(payload)
        if len(reviews) > 1:
            return {"assessed_scopes": ["job", "blueprint", "plans"], "issues": []}
        return {"assessed_scopes": ["job", "blueprint", "plans"], "issues": [{
            "id": "seeded-error", "severity": "error", "code": "unsupported_claim",
            "targets": [{"scope": "job", "target_id": "job-1", "field": "text",
                         "quote": "通过实物载荷测试验证支架承载100kg，重量降低30%。"}],
            "diagnosis": "素材明确没有进行实物测试，性能数字没有来源。",
            "instruction": "删除实测承载和减重结论，保留课程图纸工作。", "jd_quote": ""}]}
    if system == REPAIR_SYSTEM:
        before = payload["draft"]["resumes"]["job"]["items"][0]["text"]
        return {"edits": [{"issue_id": "seeded-error", "scope": "job", "target_id": "job-1",
                          "field": "text", "before": before,
                          "after": before.replace("通过实物载荷测试验证支架承载100kg，重量降低30%。", "")}]}
    if system == GUIDE_SYSTEM:
        return LocalProvider().guide(payload["kind"], payload["resume"], payload["plans"], payload["jd"])
    raise AssertionError("controlled case must not call model generation")
CompatibleProvider._call = fictional_call
output = Path(sys.argv[1])
output.mkdir()
assert evaluate(output, "controlled-repair") is True
''', output)
    assert result.returncode == 0, result.stdout + result.stderr
    summary = json.loads((output / "summary.json").read_text())
    evidence = json.loads((output / "evidence.json").read_text())
    assert summary["status"] == "passed"
    assert summary["generation_origin"] == "authored_control"
    assert summary["compatible_source_scope"] == "review_repair_guides_only"
    assert summary["review_status"] == "repaired"
    assert summary["repair_exercised"] is True
    assert summary["call_count"] == 5
    assert [call["stage"] for call in evidence["calls"]] == ["review", "repair", "review", "guide", "guide"]
    assert evidence["fixture_metadata"]["provenance"] == "authored_seeded_draft"
    assert "100kg" in evidence["before_review_draft"]["resumes"]["job"]["items"][0]["text"]
    assert "100kg" not in evidence["final"]["resumes"]["job"]["items"][0]["text"]
    assert all(summary["checks"].values())
