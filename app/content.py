from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class PlanDraft(BaseModel):
    key: str = Field(min_length=1, max_length=40)
    title: str = Field(min_length=2, max_length=100)
    target_evidence: str = Field(min_length=2)
    background: str = Field(min_length=4)
    task: str = Field(min_length=4)
    actions: list[str] = Field(min_length=1)
    outcome: str = Field(min_length=4)
    verification: str = Field(min_length=4)

    @field_validator("verification", mode="before")
    @classmethod
    def join_verification(cls, value):
        if isinstance(value, list) and value and all(isinstance(part, str) for part in value):
            return "；".join(value)
        return value


class ItemDraft(BaseModel):
    plan_key: str
    text: str = Field(min_length=12, max_length=600)
    source_quote: str = ""
    achieved: bool = False


class ResumeDraft(BaseModel):
    title: str = Field(min_length=2, max_length=100)
    summary: str = Field(min_length=8, max_length=1000)
    items: list[ItemDraft] = Field(min_length=1, max_length=10)


class BundleDraft(BaseModel):
    plans: list[PlanDraft] = Field(min_length=1, max_length=6)
    job: ResumeDraft
    blueprint: ResumeDraft


class GuideEntryDraft(BaseModel):
    item_index: int = Field(ge=0)
    question: str = Field(min_length=4)
    answer_outline: str = Field(min_length=8)
    contribution_boundary: str = Field(min_length=4)
    verification: str = Field(min_length=4)
    practice_step: str = Field(min_length=4)

    @field_validator("answer_outline", "contribution_boundary", "verification", "practice_step", mode="before")
    @classmethod
    def join_text_steps(cls, value):
        if isinstance(value, list) and value and all(isinstance(part, str) for part in value):
            return "；".join(value)
        return value


class GuideDraft(BaseModel):
    entries: list[GuideEntryDraft] = Field(min_length=1)


def stable_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def normalize_bundle(raw: dict, jd: str, material: str, confirmed: bool) -> dict:
    draft = BundleDraft.model_validate(raw)
    plan_keys = {plan.key for plan in draft.plans}
    if len(plan_keys) != len(draft.plans):
        raise ValueError("duplicate plan key")
    jd_compact = "".join(jd.split())
    plans = []
    for plan in draft.plans:
        evidence = "".join(plan.target_evidence.split())
        record = plan.model_dump()
        record["target_evidence_status"] = "jd_exact" if evidence in jd_compact else "ai_inferred"
        if record["target_evidence_status"] == "ai_inferred":
            record["target_inference"] = record["target_evidence"]
            record["target_evidence"] = ""
        plans.append(record)
    resumes: dict[str, dict] = {}
    for kind in ("job", "blueprint"):
        resume = getattr(draft, kind)
        items = []
        for index, item in enumerate(resume.items):
            if item.plan_key not in plan_keys:
                raise ValueError("unknown plan")
            quote = item.source_quote.strip()
            if quote and quote not in material:
                # Model paraphrases are not evidence. Keep the text as AI-authored,
                # but discard the invalid citation instead of presenting it as sourced.
                quote = ""
            source_status = "user_confirmed" if confirmed and quote else "user_unconfirmed" if quote else "none"
            # The body is model-authored even when it cites user-confirmed material.
            # Only an exact user-confirmed quotation can be marked achieved automatically.
            achieved = bool(confirmed and quote and item.achieved and item.text.strip() == quote)
            items.append({
                "id": f"{kind}-{index + 1}",
                "plan_key": item.plan_key,
                "text": item.text.strip(),
                "source_quote": quote,
                "source_status": source_status,
                "verification_status": "ai_generated",
                "achieved": achieved,
            })
        resumes[kind] = {"title": resume.title.strip(), "summary": resume.summary.strip(), "items": items}
    if stable_hash(resumes["job"]) == stable_hash(resumes["blueprint"]):
        raise ValueError("dual resumes identical")
    return {"plans": plans, "resumes": resumes}


def normalize_guide(raw: dict, resume: dict, kind: Literal["job", "blueprint"]) -> list[dict]:
    draft = GuideDraft.model_validate(raw)
    count = len(resume["items"])
    indexes = {entry.item_index for entry in draft.entries}
    if indexes != set(range(count)):
        raise ValueError("guide does not cover every resume item")
    if len(draft.entries) != count:
        raise ValueError("guide has duplicate entries")
    result = []
    for entry in sorted(draft.entries, key=lambda e: e.item_index):
        item = resume["items"][entry.item_index]
        if kind == "blueprint" and not item["achieved"] and not entry.practice_step.strip():
            raise ValueError("unfinished blueprint missing practice")
        if kind == "blueprint" and not item["achieved"] and re.search(r"请介绍一次|你(?:曾经|已经|当时).{0,12}(?:如何|完成|做过)|讲讲你做过", entry.question):
            raise ValueError("unfinished blueprint question implies completed experience")
        result.append({"item_id": item["id"], **entry.model_dump(exclude={"item_index"})})
    return result
