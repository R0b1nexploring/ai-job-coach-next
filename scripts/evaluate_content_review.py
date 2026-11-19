"""One synthetic, isolated real-provider evaluation through the normal service pipeline."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import logging
import os
from pathlib import Path
import sys
import time

# Support both `python scripts/...` and module invocation.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.providers import (BUNDLE_SYSTEM, GUIDE_SYSTEM, REVIEW_SYSTEM, REPAIR_SYSTEM,
                           CompatibleProvider)
from scripts.evaluate_live import JD, MATERIAL

CANARY = "SYNTHETIC_HEADER_CANARY_4d609ecb"
KINDS = ("job", "blueprint")



def controlled_fixture() -> dict:
    """Authored synthetic draft with exactly one seeded unsupported outcome."""
    quote = "个人负责支架建模、螺栓连接检查与图纸标注，最后提交课程报告。"
    return {
        "plans": [
            {"key": "course-bracket", "title": "桌面分拣装置支架课程设计",
             "target_evidence": "输出二维工程图和零件清单",
             "background": "课程设计使用桌面分拣装置支架图纸与课程报告作为输入，范围限于建模、图纸和连接检查。",
             "task": "整理本人承担的支架建模、螺栓连接检查和图纸标注工作；以已有资料为输入，拓展图纸与清单对应表、螺栓连接检查记录。",
             "actions": ["梳理支架模型与装配关系", "核对螺栓连接及图纸标注", "整理装配图、零件清单与课程报告", "拓展时逐项核对图纸与清单并记录连接检查对象及结果，此步骤待完成"],
             "outcome": "已有课程报告、支架装配图与零件清单；拓展计划形成对应表和连接检查记录；不包含实物或性能测试结果。",
             "verification": "对照课程报告、装配图和零件清单逐项解释本人负责部分。"},
            {"key": "paper-spacing", "title": "假设几何下的螺栓间距图纸比较",
             "target_evidence": "评审装配干涉、加工可行性与维护空间",
             "background": "以课程支架为参考构建纯纸面拓展，假设支架宽80毫米、四孔对称布置且孔径6毫米；这些尺寸仅为方案比较假设。",
             "task": "在上述假设下比较横向孔距40与50毫米两种二维布置的几何间隙，不进行承载或减重结论推断。",
             "actions": ["使用现有AutoCAD绘制两种假设孔距的二维草图", "手工复核孔边距并在图纸上标注螺栓装拆预留区", "记录图上可见的潜在干涉与仍需核对的装配约束"],
             "outcome": "两种假设布置的草图、尺寸复核表与取舍说明，均为待完成的纸面方案产物。",
             "verification": "通过图上尺寸与手工计算相互核对几何一致性；加工与真实装配效果仍需后续验证。"},
        ],
        "job": {"title": "求职版", "summary": "机械工程本科课程设计经历，使用AutoCAD整理支架图纸和课程报告。",
                "items": [
                    {"plan_key": "course-bracket", "text": "完成桌面分拣装置支架建模和图纸标注，整理装配图及零件清单。通过实物载荷测试验证支架承载100kg，重量降低30%。", "source_quote": quote, "achieved": False},
                    {"plan_key": "course-bracket", "text": "承担课程支架的螺栓连接检查，将检查内容整理进课程报告。", "source_quote": quote, "achieved": False},
                ]},
        "blueprint": {"title": "蓝图 Plus", "summary": "围绕课程支架图纸拓展几何比较与可检查的设计说明。",
                      "items": [
                          {"plan_key": "course-bracket", "text": "建立支架装配图、图纸标注和零件清单的逐项对应表，整理螺栓连接检查记录。", "source_quote": "", "achieved": False},
                          {"plan_key": "paper-spacing", "text": "基于宽80毫米、孔径6毫米的假设支架几何，使用AutoCAD比较40与50毫米横向孔距，手工复核孔边距并标注装拆预留区，输出二维草图、复核表与取舍说明。", "source_quote": "", "achieved": False},
                      ]},
    }

def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def progress(event: str, **fields) -> None:
    print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def safe_error(exc: Exception) -> dict:
    # Never include str(exc), response bodies, request URLs, or configuration.
    import httpx
    category = type(exc).__name__
    result = {"category": category if category in {
        "ValueError", "TypeError", "KeyError", "IndexError", "JSONDecodeError",
        "HTTPStatusError", "ReadTimeout", "ConnectTimeout", "ConnectError",
        "RemoteProtocolError", "OSError", "RuntimeError",
    } else "evaluation_error"}
    if isinstance(exc, httpx.HTTPStatusError):
        result["http_status"] = exc.response.status_code
    return result


def load_settings(path: Path | None) -> None:
    legacy = {"LLM_API_BASE_URL": "COACH_API_BASE_URL", "LLM_API_KEY": "COACH_API_KEY",
              "LLM_MODEL_STRONG": "COACH_MODEL"}
    values = {}
    if path:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            if key.startswith("COACH_") or key in legacy:
                values[key] = value.strip().strip("\"'")
        for key, value in values.items():
            if key.startswith("COACH_"):
                os.environ[key] = value
    for old, new in legacy.items():
        if new not in values and (values.get(old) or os.environ.get(old)):
            os.environ[new] = values.get(old) or os.environ[old]


class TrackingProvider(CompatibleProvider):
    def __init__(self, output: Path, case: str = "natural"):
        super().__init__()
        self.output = output
        self.case = case
        self.fixture_metadata = None
        self.calls: list[dict] = []
        self.guide_inputs: list[dict] = []

    def bundle(self, jd: str, material: str) -> dict:
        if self.case == "natural":
            return super().bundle(jd, material)
        raw = controlled_fixture()
        self.fixture_metadata = {
            "provenance": "authored_seeded_draft", "initial_seeded_raw": deepcopy(raw),
            "expected_defect": {"scope": "job", "target_id": "job-1", "field": "text",
                                "quote": "通过实物载荷测试验证支架承载100kg，重量降低30%。"},
        }
        return raw

    def _call(self, system: str, user: str) -> dict:
        stage = {BUNDLE_SYSTEM: "bundle", GUIDE_SYSTEM: "guide",
                 REVIEW_SYSTEM: "review", REPAIR_SYSTEM: "repair"}.get(system, "unknown")
        correction = "上一版 JSON 未通过" in user
        record = {"number": len(self.calls) + 1, "stage": stage,
                  "correction": correction, "system": system, "user": user}
        self.calls.append(record)
        progress("call_started", number=record["number"], stage=stage, correction=correction)
        started = time.monotonic()
        try:
            raw = super()._call(system, user)
            record["raw_output"] = deepcopy(raw)
            return raw
        except Exception as exc:
            record["error"] = safe_error(exc)
            raise
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - started, 3)
            write_json(self.output / "calls.json", self.calls)
            progress("call_finished", number=record["number"], stage=stage,
                     elapsed_seconds=record["elapsed_seconds"], error=record.get("error"))

    def guide(self, kind: str, resume: dict, plans: list[dict], jd: str) -> dict:
        self.guide_inputs.append(deepcopy({"kind": kind, "resume": resume, "plans": plans, "jd": jd}))
        return super().guide(kind, resume, plans, jd)


def evaluate(output: Path, case: str = "natural") -> bool:
    # app.db owns a module-level engine. Override every database route before importing it.
    os.environ["COACH_DATABASE_URL"] = "sqlite:///" + str(output / "evaluation.sqlite")
    os.environ["COACH_DATA_DIR"] = str(output)
    os.environ["COACH_JOURNAL_DIR"] = str(output / "deletion-journal")
    from sqlalchemy import select
    from app.db import Base, engine, SessionLocal
    from app.models import TaskExecution
    from app.service import (create_batch, process_batch_once, process_guide_once, get_batch,
                             list_revisions, list_guide_revisions, list_executions, semantic_hash)

    logging.disable(logging.CRITICAL)  # The artifact below uses only controlled error fields.
    Base.metadata.create_all(engine)
    from app.privacy import initialize_journal
    initialize_journal(SessionLocal, bootstrap=True)
    generation_origin = "model" if case == "natural" else "authored_control"
    evidence = {"case": case, "generation_origin": generation_origin, "synthetic": True, "jd": JD, "material": MATERIAL, "material_confirmed": True}
    provider = None
    batch = None
    failure = None
    started = time.monotonic()
    try:
        provider = TrackingProvider(output, case)
        batch = create_batch(SessionLocal, jd=JD, material=MATERIAL, confirmed=True,
                             idempotency_key="synthetic-content-review-evaluation",
                             profile={"name": CANARY, "email": "synthetic@example.invalid"})
        process_batch_once(SessionLocal, provider, progress=lambda: progress("pipeline_checkpoint"))
        state = get_batch(SessionLocal, batch.id)
        if state["status"] == "guides_pending":
            for _ in KINDS:
                if not process_guide_once(SessionLocal, provider):
                    break
                state = get_batch(SessionLocal, batch.id)
                if any(g["status"] == "failed" for g in state["guides"].values()):
                    break
    except Exception as exc:
        failure = safe_error(exc)
    finally:
        calls = provider.calls if provider else []
        evidence.update(fixture_metadata=provider.fixture_metadata if provider else None, calls=calls, guide_inputs=provider.guide_inputs if provider else [], error=failure)
        checks = {}
        if batch:
            state = get_batch(SessionLocal, batch.id)
            evidence["final"] = state
            evidence["resume_revisions"] = {k: list_revisions(SessionLocal, batch.id, k)
                for k in KINDS if k in state["resumes"]}
            evidence["guide_revisions"] = {k: list_guide_revisions(SessionLocal, batch.id, k)
                for k in KINDS if k in state["guides"]}
            evidence["executions"] = list_executions(SessionLocal, batch.id)
            with SessionLocal() as session:
                evidence["immutable_inputs"] = [{"execution_id": row.id, "task_type": row.task_type,
                    "input_hash": row.input_hash, "input_snapshot": deepcopy(row.input_snapshot)}
                    for row in session.scalars(select(TaskExecution).where(TaskExecution.batch_id == batch.id))]
            report = (state.get("quality") or {}).get("content_review") or {}
            evidence["content_review"] = report
            review_calls = [call for call in calls if call["stage"] == "review"]
            evidence["before_review_draft"] = json.loads(review_calls[0]["user"])["draft"] if review_calls else None
            checks.update(batch_ready=state["status"] == "ready",
                          review_accepted=report.get("status") in {"passed", "repaired", "suggestions"},
                          source_compatible=state.get("source") == "compatible" and report.get("source") == "compatible")
            checks["both_versions"] = set(state["resumes"]) == set(KINDS) == set(state["guides"])
            for kind in KINDS:
                resume, guide = state["resumes"].get(kind), state["guides"].get(kind)
                checks[kind + "_present"] = bool(resume and guide)
                if not resume or not guide:
                    continue
                expected_resume = {field: deepcopy(resume[field]) for field in ("title", "summary", "items")}
                inputs = [item for item in evidence["guide_inputs"] if item["kind"] == kind]
                revisions = evidence["guide_revisions"][kind]
                rr = evidence["resume_revisions"][kind]
                ids = [item["id"] for item in resume["items"]]
                entries = [entry["item_id"] for entry in guide["entries"]]
                checks[kind + "_coverage"] = len(entries) == len(set(entries)) == len(ids) and set(entries) == set(ids)
                checks[kind + "_source"] = guide.get("source") == "compatible" and guide["status"] == "ready" and all(r["source"] == "compatible" for r in revisions)
                checks[kind + "_guide_input"] = inputs == [{"kind": kind, "jd": JD, "resume": expected_resume, "plans": resume["plans"]}]
                checks[kind + "_semantic_hash"] = guide["semantic_hash"] == resume["semantic_hash"] == semantic_hash(expected_resume, resume["plans"], JD)
                frozen = [row["input_snapshot"] for row in evidence["immutable_inputs"]
                          if row["task_type"] == "guide" and row["input_snapshot"]["kind"] == kind]
                checks[kind + "_execution_input"] = len(frozen) == 1 and all(
                    row["resume"] == expected_resume and row["plans"] == resume["plans"]
                    and row["jd"] == JD and row["semantic_hash"] == resume["semantic_hash"]
                    for row in frozen)
                checks[kind + "_frozen_revisions"] = bool(revisions and rr) and all(
                    r["resume"] == expected_resume and r["plans"] == resume["plans"] and r["jd_text"] == JD
                    and r["semantic_hash"] == resume["semantic_hash"] and r["resume_revision"] == resume["revision"]
                    for r in revisions) and all(all(r[f] == resume[f] for f in ("title", "summary", "header", "items", "plans", "semantic_hash")) for r in rr)
            checks["blueprint_unachieved"] = bool(state["resumes"].get("blueprint")) and all(item["achieved"] is False for item in state["resumes"]["blueprint"]["items"])
            failure = failure or ({"category": "service_failure", "reason": report.get("reason") or state["error_code"]} if state["status"] != "ready" else None)
        checks["header_excluded"] = CANARY not in json.dumps(calls, ensure_ascii=False)
        checks["no_call_errors"] = not any("error" in call for call in calls)
        passed = bool(batch) and failure is None and all(checks.values())
        summary = {"case": case, "generation_origin": generation_origin,
                   "compatible_source_scope": "generation_review_guides" if case == "natural" else "review_repair_guides_only",
                   "synthetic": True, "status": "passed" if passed else "failed", "checks": checks,
                   "model": provider.model if provider else None, "provider": "compatible",
                   "call_count": len(calls), "call_error_count": sum("error" in call for call in calls),
                   "repair_exercised": any(call["stage"] == "repair" for call in calls),
                   "review_status": evidence.get("content_review", {}).get("status"),
                   "error": failure, "elapsed_seconds": round(time.monotonic() - started, 3)}
        evidence["summary"] = summary
        write_json(output / "evidence.json", evidence)
        write_json(output / "summary.json", summary)
        engine.dispose()
        progress("evaluation_finished", **summary)
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("natural", "controlled-repair"), default="natural")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.expanduser().absolute()
    if output.is_symlink() or (output.exists() and (not output.is_dir() or any(output.iterdir()))):
        progress("evaluation_rejected", reason="output_must_be_new_or_empty_directory")
        return 2
    output.mkdir(parents=True, exist_ok=True)
    try:
        load_settings(args.env_file)
        return 0 if evaluate(output, args.case) else 1
    except Exception as exc:
        summary = {"status": "failed", "synthetic": True, "error": safe_error(exc)}
        write_json(output / "summary.json", summary)
        write_json(output / "evidence.json", summary)
        progress("evaluation_failed", **summary)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
