"""Exercise a running API with new synthetic batches; never read or delete user data."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlsplit
from uuid import uuid4

from docx import Document
import httpx
from pypdf import PdfReader


JD = """虚构验收岗位：机械结构设计工程师
负责机械结构方案、装配图和零件清单，说明设计约束与个人贡献。
使用 CAD 工具检查装配关系，维护修改记录，向团队解释取舍及验证方法。
能够按可用设备和工具规划设计检查，不将设计指标当作实测成果。"""
MATERIAL = """以下材料完全虚构，仅用于系统验收，没有真实姓名、联系方式或企业。
机械专业课程项目：设计桌面分拣装置的支架。
本人使用 CAD 绘制支架零件图和装配图，整理 BOM 与课程汇报材料，比较两种连接方式。
保留了图纸和修改记录，没有实际加工、有限元仿真、企业任职或实测性能数据。"""
EDITED_ITEM = "虚构验收修改：本人将支架方案改为螺栓连接，更新 CAD 装配图与 BOM，并记录尺寸核对方法；尚无实测成果。"
NEW_ITEM = "虚构验收新增项目：编写 Python 检查表核对图纸与零件清单版本，输出差异记录和待核对项，不声称已经完成性能测试。"
KINDS = ("job", "blueprint")
INTERNAL_LABELS = re.compile(r"\b(?:verification_status|source_status|source_quote|achieved|plan_key|item_index)\b")


class DeliveryError(RuntimeError):
    pass


def require(condition: bool, label: str) -> None:
    if not condition:
        raise DeliveryError(label)


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


class VisibleText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hidden = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in {"style", "script", "head"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"style", "script", "head"}:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def html_text(data: bytes) -> str:
    parser = VisibleText()
    parser.feed(data.decode("utf-8"))
    return "\n".join(parser.parts)


def require_saved_text(text: str, resume: dict, label: str) -> None:
    visible = compact(text)
    cursor = 0
    for part in [resume["title"], resume["summary"], "项目与能力"] + [item["text"] for item in resume["items"]]:
        match = compact(part)
        position = visible.find(match, cursor)
        require(position >= cursor, f"{label}: saved text missing or out of order")
        cursor = position + len(match)
    require(INTERNAL_LABELS.search(text) is None, f"{label}: internal labels visible")


def validate_batch(batch: dict, expected_source: str) -> None:
    require(batch["status"] == "ready", "batch not ready")
    require(batch.get("source") == expected_source, "batch used an unexpected provider or fallback")
    require(set(batch["resumes"]) == set(KINDS), "dual resumes missing")
    require(set(batch["guides"]) == set(KINDS), "dual guides missing")
    for kind in KINDS:
        resume, guide = batch["resumes"][kind], batch["guides"][kind]
        require(guide["status"] == "ready" and guide.get("source") == expected_source, f"{kind}: guide unavailable or used fallback")
        item_ids = [item["id"] for item in resume["items"]]
        references = [entry["item_id"] for entry in guide["entries"]]
        require(item_ids and len(item_ids) == len(set(item_ids)), f"{kind}: invalid item ids")
        require(sorted(references) == sorted(item_ids), f"{kind}: guide coverage mismatch")
        require(guide.get("semantic_hash") == resume.get("semantic_hash"), f"{kind}: guide semantic binding mismatch")
        require(guide.get("applies_to_revision") == resume["revision"], f"{kind}: guide revision mismatch")
        plans = {plan["key"] for plan in resume["plans"]}
        require(all(item["plan_key"] in plans for item in resume["items"]), f"{kind}: plan reference missing")
        if kind == "blueprint":
            require(all(item.get("achieved") is False for item in resume["items"]), "blueprint claims synthetic goals achieved")
        public = [resume["title"], resume["summary"]] + [item["text"] for item in resume["items"]]
        public.extend(value for entry in guide["entries"] for field, value in entry.items() if field != "item_id" and isinstance(value, str))
        require(all(INTERNAL_LABELS.search(text) is None for text in public), f"{kind}: internal labels in visible content")


class DeliveryCheck:
    def __init__(self, base_url: str, output: Path, expected_source: str, timeout: float):
        self.output, self.expected_source, self.timeout = output, expected_source, timeout
        self.client = httpx.Client(base_url=base_url, timeout=min(timeout, 30), trust_env=False)

    def request(self, method: str, path: str, *, status: int = 200, **kwargs) -> httpx.Response:
        response = self.client.request(method, path, **kwargs)
        require(response.status_code == status, f"{method} {path}: expected HTTP {status}, got {response.status_code}")
        return response

    def get(self, path: str):
        return self.request("GET", path).json()

    def write_json(self, path: Path, data) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def wait_ready(self, batch_id: str) -> dict:
        deadline, last_status = time.monotonic() + self.timeout, None
        while time.monotonic() < deadline:
            try:
                batch = self.get(f"/api/batches/{batch_id}")
            except (httpx.ConnectError, httpx.ReadError, httpx.ReadTimeout):
                time.sleep(min(1, max(0, deadline - time.monotonic())))
                continue
            status = batch["status"]
            if status != last_status:
                print(f"[batch] {batch_id} {status}", flush=True)
                last_status = status
            if status == "ready":
                return batch
            require(status not in {"failed", "guides_partial"}, f"batch {batch_id}: terminal failure {status}")
            time.sleep(min(1, max(0, deadline - time.monotonic())))
        raise DeliveryError(f"batch {batch_id}: timeout waiting for ready")

    def download_resume(self, batch_id: str, kind: str, resume: dict, directory: Path, *, historical=False) -> list[dict]:
        artifacts = []
        suffix = f"?revision={resume['revision']}" if historical else ""
        for format in ("html", "docx", "pdf"):
            response = self.request("GET", f"/api/batches/{batch_id}/resumes/{kind}/download/{format}{suffix}")
            require(f"v{resume['revision']}.{format}" in response.headers.get("content-disposition", ""), "resume filename version mismatch")
            filename = f"{'historical-' if historical else ''}{kind}-v{resume['revision']}.{format}"
            path = directory / filename
            path.write_bytes(response.content)
            info = {"file": filename, "bytes": len(response.content), "sha256": sha256(response.content).hexdigest()}
            if format == "html":
                text = html_text(response.content)
            elif format == "docx":
                doc = Document(BytesIO(response.content))
                text = "\n".join(paragraph.text for paragraph in doc.paragraphs)
                require(abs(doc.sections[0].page_width.cm - 21) < 0.01 and abs(doc.sections[0].page_height.cm - 29.7) < 0.01, "DOCX is not A4")
            else:
                pdf = PdfReader(BytesIO(response.content))
                require(bool(pdf.pages), "PDF has no pages")
                text = "\n".join(page.extract_text() for page in pdf.pages)
                info["pages"] = len(pdf.pages)
                for page in pdf.pages:
                    require(abs(float(page.mediabox.width) - 595.28) < 1 and abs(float(page.mediabox.height) - 841.89) < 1, "PDF is not A4")
                    for reference in page["/Resources"]["/Font"].values():
                        font = reference.get_object()
                        require("/FontDescriptor" in font and "/FontFile2" in font["/FontDescriptor"] and "/ToUnicode" in font, "PDF font is not embedded or lacks Unicode mapping")
            require_saved_text(text, resume, filename)
            artifacts.append(info)
        return artifacts

    def download_guide(self, batch_id: str, kind: str, guide: dict, directory: Path, *, historical=False) -> dict:
        route = f"/api/batches/{batch_id}/guides/{kind}"
        if historical:
            route += f"/revisions/{guide['revision']}"
        response = self.request("GET", route + "/download")
        text = html_text(response.content)
        require(INTERNAL_LABELS.search(text) is None, "guide export contains internal labels")
        for entry in guide["entries"]:
            for field in ("question", "answer_outline", "contribution_boundary", "verification", "practice_step"):
                require(compact(entry[field]) in compact(text), "guide export omitted saved content")
        require(f"resume-v{guide['resume_revision']}" in response.headers.get("content-disposition", ""), "guide filename binding mismatch")
        filename = f"{'historical-' if historical else ''}{kind}-guide-g{guide['revision']}-resume-v{guide['resume_revision']}.html"
        (directory / filename).write_bytes(response.content)
        return {"file": filename, "bytes": len(response.content), "sha256": sha256(response.content).hexdigest()}

    def run_case(self, case: str, run_id: str, resume_batch: str | None = None) -> dict:
        directory = self.output / case
        directory.mkdir()
        material = "" if case == "empty" else MATERIAL
        if case == "upload":
            extracted = self.request("POST", "/api/materials/extract", files={"file": ("synthetic-material.txt", MATERIAL.encode(), "text/plain")}).json()
            require(extracted["text"] == MATERIAL, "TXT extraction changed synthetic input")
            material = extracted["text"]
        payload = {"jd_text": JD, "material_text": material, "material_confirmed": False}
        self.write_json(directory / "input.json", {"synthetic": True, **payload})
        headers = {"Idempotency-Key": f"delivery-{run_id}-{case}"}
        if resume_batch:
            batch_id = resume_batch
            frozen = self.get(f"/api/batches/{batch_id}")
            require(frozen["jd_text"] == JD and frozen["material_text"] == material and not frozen["material_confirmed"], "resume target is not this script's synthetic fixture")
            require(all(row["revision"] == 1 for row in frozen["resumes"].values()), "resume fixture has already been edited")
        else:
            created = self.request("POST", "/api/batches", status=202, json=payload, headers=headers).json()
            replay = self.request("POST", "/api/batches", status=202, json=payload, headers=headers).json()
            require(replay["id"] == created["id"], "idempotent API replay created a second batch")
            batch_id = created["id"]
        initial = self.wait_ready(batch_id)
        self.write_json(directory / "initial.json", initial)
        validate_batch(initial, self.expected_source)
        old_resume = deepcopy(initial["resumes"]["job"])
        old_guide_revision = initial["guides"]["job"]["revision"]
        items = deepcopy(old_resume["items"])
        require(len(items) < 20, "synthetic resume has no room for an added item")
        items[0]["text"] = EDITED_ITEM
        items.append({"text": NEW_ITEM})
        saved = self.request("PUT", f"/api/batches/{batch_id}/resumes/job", json={"revision": old_resume["revision"], "title": old_resume["title"], "summary": old_resume["summary"], "items": items}).json()
        self.write_json(directory / "edited.json", saved)
        require(saved["guides"]["job"]["status"] == "stale", "edited guide not stale")
        require(saved["resumes"]["blueprint"] == initial["resumes"]["blueprint"] and saved["guides"]["blueprint"] == initial["guides"]["blueprint"], "single-version edit changed blueprint")
        new_resume = saved["resumes"]["job"]
        require(new_resume["revision"] == old_resume["revision"] + 1, "saved edit did not create a revision")
        plan_map = {plan["key"]: plan for plan in new_resume["plans"]}
        require(all(item["plan_key"] in plan_map for item in new_resume["items"]), "added item has no plan snapshot")
        require(EDITED_ITEM in plan_map[new_resume["items"][0]["plan_key"]]["actions"], "edited plan retains old actions")
        added = next((item for item in new_resume["items"] if item["text"] == NEW_ITEM), None)
        require(added is not None and NEW_ITEM in plan_map[added["plan_key"]]["actions"], "new plan does not match new item")
        self.request("POST", f"/api/batches/{batch_id}/guides/job/retry", status=202)
        final = self.wait_ready(batch_id)
        self.write_json(directory / "final.json", final)
        validate_batch(final, self.expected_source)
        guide_history = self.get(f"/api/batches/{batch_id}/guides/job/revisions")
        self.write_json(directory / "guide-history.json", guide_history)
        original_guide = next((guide for guide in guide_history if guide["revision"] == old_guide_revision), None)
        require(original_guide is not None and original_guide["status"] == "ready", "old ready guide was lost")
        require(original_guide["resume_revision"] == old_resume["revision"] and original_guide["resume"]["items"] == old_resume["items"], "historical guide is paired with edited resume")
        artifacts = self.download_resume(batch_id, "job", old_resume, directory, historical=True)
        artifacts.append(self.download_guide(batch_id, "job", original_guide, directory, historical=True))
        for kind in KINDS:
            artifacts += self.download_resume(batch_id, kind, final["resumes"][kind], directory)
            artifacts.append(self.download_guide(batch_id, kind, final["guides"][kind], directory))
        archive = self.request("GET", f"/api/batches/{batch_id}/archive")
        (directory / "archive.json").write_bytes(archive.content)
        require(archive.json()["batch"]["id"] == batch_id, "archive belongs to a different batch")
        return {"case": case, "batch_id": batch_id, "status": "passed", "source": final["source"], "resume_items": {kind: len(final["resumes"][kind]["items"]) for kind in KINDS}, "resume_revisions": {kind: final["resumes"][kind]["revision"] for kind in KINDS}, "guide_revisions": {kind: final["guides"][kind]["revision"] for kind in KINDS}, "artifacts": artifacts}


def main() -> int:
    parser = argparse.ArgumentParser(description="用新建虚构批次验证正在运行的双版本交付，不读取或删除现有个人数据")
    parser.add_argument("--base-url", default="http://127.0.0.1:18827")
    parser.add_argument("--expected-source", choices=("local", "compatible"), default="local")
    parser.add_argument("--cases", choices=("all", "single"), default="all")
    parser.add_argument("--timeout", type=float, default=900, help="每个后台等待阶段的秒数")
    parser.add_argument("--output", type=Path, default=Path("data/delivery-smoke"))
    parser.add_argument("--resume-batch", help="继续本脚本刚创建且未编辑的虚构批次，避免连接中断后重复模型调用")
    args = parser.parse_args()
    if args.resume_batch and args.cases != "single":
        parser.error("--resume-batch requires --cases single")
    url = urlsplit(args.base_url)
    require(url.scheme in {"http", "https"} and bool(url.netloc) and url.username is None and url.password is None and not url.query and not url.fragment, "invalid base URL")
    require(args.timeout > 0, "timeout must be positive")
    run_id = uuid4().hex
    output = args.output.resolve() / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + run_id[:8])
    output.mkdir(parents=True)
    check = DeliveryCheck(args.base_url.rstrip("/"), output, args.expected_source, args.timeout)
    report = {"format_version": 1, "synthetic": True, "expected_source": args.expected_source, "scope": "HTTP delivery, not real user quality or public deployment", "cases": [], "status": "running"}
    try:
        report["health"] = check.get("/api/health")
        for case in (("manual", "upload", "empty") if args.cases == "all" else ("manual",)):
            report["cases"].append(check.run_case(case, run_id, args.resume_batch))
        report["status"] = "passed"
        return_code = 0
    except (DeliveryError, httpx.HTTPError, ValueError, KeyError) as error:
        report["status"] = "failed"
        report["error"] = str(error) if isinstance(error, DeliveryError) else type(error).__name__
        return_code = 1
    finally:
        check.client.close()
        check.write_json(output / "report.json", report)
    print(json.dumps({"status": report["status"], "cases": [{"case": case["case"], "batch_id": case["batch_id"], "source": case["source"]} for case in report["cases"]], "report": str(output / "report.json"), "error": report.get("error")}, ensure_ascii=False))
    return return_code


if __name__ == "__main__":
    sys.exit(main())
