from __future__ import annotations

import os
import re
import time
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from app.content import BundleDraft, GuideDraft, normalize_bundle, normalize_guide


class Provider(Protocol):
    name: str
    def bundle(self, jd: str, material: str) -> dict: ...
    def guide(self, kind: str, resume: dict, plans: list[dict], jd: str) -> dict: ...


def jd_evidence(jd: str) -> str:
    lines = [line.strip(" -•\t") for line in jd.splitlines() if len(line.strip(" -•\t")) >= 4]
    return (lines[0] if lines else jd.strip())[:100]


class LocalProvider:
    name = "local"

    def bundle(self, jd: str, material: str) -> dict:
        evidence = jd_evidence(jd)
        material_line = next((line.strip(" -•\t") for line in material.splitlines() if len(line.strip()) >= 8), "")
        has_material = bool(material_line)
        project_title = "相关经历整理" if has_material else "岗位项目规划"
        plan = {
            "key": "project-1", "title": project_title, "target_evidence": evidence,
            "background": f"目标岗位要求：{evidence}。需要明确问题和工作边界。",
            "task": "梳理个人可承担的任务，并说明输入、输出与限制。",
            "actions": ["列出问题、可用资料和工具", "形成可检查的方案或工作记录"],
            "outcome": "形成项目方案和可供讨论的产物；尚无实测结果。",
            "verification": "通过方案文档、过程记录和针对岗位要求的追问检查。",
        }
        job_text = (
            f"围绕“{evidence}”梳理相关经历：{material_line}；归纳本人任务、使用的方法和可核对的产物。"
            if has_material else f"围绕“{evidence}”制定个人项目方案，梳理问题、任务、实施步骤和预期产物。"
        )
        blueprint_text = f"针对“{evidence}”设计可实施项目：明确约束，分步骤完成方案与记录，并用可检查产物验证。"
        return {
            "plans": [plan],
            "job": {"title": "求职版", "summary": "面向目标岗位整理现有材料与可讲述的项目思路。", "items": [{"plan_key": "project-1", "text": job_text, "source_quote": material_line, "achieved": has_material}]},
            "blueprint": {"title": "蓝图 Plus", "summary": "围绕目标岗位规划下一步可完成的项目与能力提升。", "items": [{"plan_key": "project-1", "text": blueprint_text, "source_quote": "", "achieved": False}]},
        }

    def guide(self, kind: str, resume: dict, plans: list[dict], jd: str) -> dict:
        entries = []
        for index, item in enumerate(resume["items"]):
            unfinished = not item["achieved"]
            plan = next((plan for plan in plans if plan.get("key") == item.get("plan_key")), plans[0])
            entries.append({
                "item_index": index,
                "question": f"你会如何完成这项内容并回应岗位要求：{jd_evidence(jd)}？" if kind == "blueprint" and unfinished else f"请说明这项内容如何回应岗位要求：{jd_evidence(jd)}？",
                "answer_outline": ("先说明问题和个人任务，再讲具体方法、产物与验证；尚未完成的步骤只描述计划。" if unfinished else "说明实际背景、本人行动、产物以及可以核对的结果。") + f"对应要点：{item['text']}；方案任务：{plan['task']}；预期产物：{plan['outcome']}",
                "contribution_boundary": "区分本人完成的部分、团队工作和下一步计划。",
                "verification": plan["verification"],
                "practice_step": "；".join(plan["actions"]) + "；整理可展示的方案、记录或样例，并练习解释关键取舍。",
            })
        return {"entries": entries}


BUNDLE_SYSTEM = """你是中文理工科求职材料写作助手。只返回严格 JSON，无 Markdown。一次构思共享项目方案，再生成求职版 job 和蓝图版 blueprint。
项目方案 plans 每项包含 key,title,target_evidence,background,task,actions, outcome,verification。target_evidence 必须逐字来自 JD。verification 可为一个字符串或字符串数组；actions 必须是字符串数组。
两版包含 title,summary,items。每条 item 包含 plan_key,text,source_quote,achieved。source_quote 必须是一个字符串，achieved 必须是布尔值。source_quote 必须是用户素材中的连续原文；无来源则空字符串。
求职版正文应像可投递简历，只写与已提供经历相关的个人行动、产物和岗位价值。标题、简介和要点都不要列举“不具备的经验”“尚未使用的软件”“未完成的岗位要求”，也不要写“未覆盖、后续补充、需要完善、建议”等评语；这些差距和核对事项放进面试指南。不能为了回避差距而反过来声称做过未做过的事。无素材时可给可编辑的项目规划稿，但不能伪称已完成。
蓝图版提出更完整、可实现的项目与验证路径。蓝图正文按简历语气写具体项目行动和产物，不写面向作者的说明、免责声明、使用建议或“完成前不应写入简历”等元评论；内部 achieved=false 和来源状态会由应用保存。
不得杜撰姓名、学校、公司任职、证书、日期或实测数字。设计指标与预期成果不能写成已发生。用具体行动和产物写作；不要空泛技能清单。"""

GUIDE_SYSTEM = """你是中文面试准备助手。只返回严格 JSON，无 Markdown。逐条对应给定简历 item，返回 entries，每条有 item_index,question,answer_outline,contribution_boundary,verification,practice_step。item_index 从 0 开始，每条简历 item 恰有一条指南。
回答框架必须与简历正文及项目方案一致。source_quote 代表用户提供的材料，source_status 表示其确认状态；即使来源已确认，模型扩写句仍需核对。未完成或 AI 补充的内容不能写成用户已亲历的第一人称经历；指出需要核对和实践的地方。蓝图版要给实际可执行的练习步骤。
所有可见文案用自然中文，不要输出 source_status、verification_status、source_quote、achieved、plan_key、item_index 等程序字段名，也不要使用 P1/P2 之类未解释的项目代号。用项目名称或要点内容直接指代。
蓝图 item 的 achieved=false 时，问题必须用“如果要完成……你会如何……”或“你会如何……”等假设式提问；不要问“请介绍一次你如何做过……”或其他暗示已经完成的经历。"""


class CompatibleProvider:
    name = "compatible"

    def __init__(self) -> None:
        self.base_url = os.getenv("COACH_API_BASE_URL", "").rstrip("/")
        self.key = os.getenv("COACH_API_KEY", "")
        self.model = os.getenv("COACH_MODEL", "")
        self.timeout = float(os.getenv("COACH_TIMEOUT_SECONDS", "90"))
        if not self.base_url or not self.model:
            raise ValueError("compatible provider requires base URL and model")
        endpoint = urlsplit(self.base_url)
        if endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
            raise ValueError("provider URL must not contain credentials, query or fragment")
        if endpoint.scheme != "https" and not (endpoint.scheme == "http" and endpoint.hostname in {"127.0.0.1", "localhost", "::1"}):
            raise ValueError("remote provider URL must use HTTPS")
        if not endpoint.hostname or not 1 <= self.timeout <= 120:
            raise ValueError("invalid provider configuration")

    def _call(self, system: str, user: str) -> dict:
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        for attempt in range(3):
            response = httpx.post(
                f"{self.base_url}/chat/completions",
                headers=headers,
                json={"model": self.model, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": 0.3, "response_format": {"type": "json_object"}},
                timeout=self.timeout,
            )
            if response.status_code not in {429, 500, 502, 503, 504} or attempt == 2:
                break
            # A bounded delay avoids retry storms. Responses and user materials
            # are deliberately never included in diagnostic messages.
            time.sleep(2 ** attempt)
        response.raise_for_status()
        payload = response.json()
        choice = payload["choices"][0]
        if choice.get("finish_reason") in {"length", "content_filter"}:
            raise ValueError("model response incomplete")
        content = choice["message"]["content"]
        if not isinstance(content, str) or not content.strip() or len(content) > 200000:
            raise ValueError("empty model output")
        import json
        return json.loads(content)

    def bundle(self, jd: str, material: str) -> dict:
        import json
        user = f"JD:\n{jd}\n\n用户素材:\n{material or '无'}"
        raw = self._call(BUNDLE_SYSTEM, user)
        try:
            normalize_bundle(raw, jd, material, False)
            return raw
        except (ValueError, TypeError):
            correction = user + "\n\n上一版 JSON 未通过格式、JD 原文证据、素材原文引用或 plan_key 对应检查。请完整重新输出，确保每个 item.plan_key 恰好对应 plans 中存在的 key，verification 是字符串，source_quote 是素材的连续原文。上一版：\n" + json.dumps(raw, ensure_ascii=False)
            corrected = self._call(BUNDLE_SYSTEM, correction)
            normalize_bundle(corrected, jd, material, False)
            return corrected

    def guide(self, kind: str, resume: dict, plans: list[dict], jd: str) -> dict:
        import json
        user = json.dumps({"kind": kind, "jd": jd, "resume": resume, "plans": plans}, ensure_ascii=False)
        raw = self._call(GUIDE_SYSTEM, user)
        try:
            normalize_guide(raw, resume, kind)
            return raw
        except (ValueError, TypeError):
            correction = user + "\n\n上一版 JSON 未通过指南结构或覆盖检查。请重新输出，entries 对每个 item_index 从 0 起恰好有一条，不能多也不能少。上一版：\n" + json.dumps(raw, ensure_ascii=False)
            corrected = self._call(GUIDE_SYSTEM, correction)
            normalize_guide(corrected, resume, kind)
            return corrected


class FallbackProvider:
    def __init__(self, primary: Provider, fallback: Provider | None = None) -> None:
        self.primary = primary
        self.fallback = fallback or LocalProvider()
        self.name = primary.name
        self.last_source = primary.name

    def bundle(self, jd: str, material: str) -> dict:
        try:
            raw = self.primary.bundle(jd, material)
            BundleDraft.model_validate(raw)
            self.last_source = self.primary.name
            return raw
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            self.last_source = "degraded_local"
            return self.fallback.bundle(jd, material)

    def guide(self, kind: str, resume: dict, plans: list[dict], jd: str) -> dict:
        try:
            raw = self.primary.guide(kind, resume, plans, jd)
            GuideDraft.model_validate(raw)
            self.last_source = self.primary.name
            return raw
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
            self.last_source = "degraded_local"
            return self.fallback.guide(kind, resume, plans, jd)


def build_provider() -> Provider:
    name = os.getenv("COACH_PROVIDER", "local")
    if name == "compatible":
        return FallbackProvider(CompatibleProvider())
    if name != "local":
        raise ValueError("unknown provider")
    return LocalProvider()
