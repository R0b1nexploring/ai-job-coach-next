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

    @field_validator("key", "title", "target_evidence", "background", "task", "outcome", "verification", mode="before")
    @classmethod
    def trim_text_fields(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("verification", mode="before")
    @classmethod
    def join_verification(cls, value):
        if isinstance(value, list) and value and all(isinstance(part, str) for part in value):
            return "；".join(value)
        return value

    @field_validator("actions", mode="before")
    @classmethod
    def trim_actions(cls, value):
        if isinstance(value, list):
            actions = [action.strip() if isinstance(action, str) else action for action in value]
            if any(not action for action in actions if isinstance(action, str)):
                raise ValueError("plan actions must not be blank")
            return actions
        return value


class ItemDraft(BaseModel):
    plan_key: str
    text: str = Field(min_length=12, max_length=600)
    source_quote: str = ""
    achieved: bool = False

    @field_validator("plan_key", mode="before")
    @classmethod
    def trim_plan_key(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("text", mode="before")
    @classmethod
    def trim_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class ResumeDraft(BaseModel):
    title: str = Field(min_length=2, max_length=100)
    summary: str = Field(min_length=8, max_length=1000)
    items: list[ItemDraft] = Field(min_length=1, max_length=10)

    @field_validator("title", "summary", mode="before")
    @classmethod
    def trim_text_fields(cls, value):
        return value.strip() if isinstance(value, str) else value


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
            return "；".join(value).strip()
        return value

    @field_validator("question", "answer_outline", "contribution_boundary", "verification", "practice_step", mode="before")
    @classmethod
    def trim_text_fields(cls, value):
        return value.strip() if isinstance(value, str) else value


class GuideDraft(BaseModel):
    entries: list[GuideEntryDraft] = Field(min_length=1)


def stable_hash(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


# Visible quality signals. These never block generation; they surface what the
# model could not ground in user material so the UI and reports can show it.
GAP_PHRASES = ("不具备", "尚未", "未掌握", "未覆盖", "需要完善", "后续补充", "待完善", "待补充")
KNOWN_ITEM_FLAGS = {"dropped_source_quote", "achievement_unverified"}

# Internal program field names must never reach user-facing guide text. Unlike
# quality flags, a leak rejects the draft and triggers the provider's single
# correction round. Only snake_case tokens are rejected: bare English words and
# "P2"-style codes can legitimately appear in mixed-language user material, so
# flagging them would false-positive. Those stay prompt-level constraints.
_GUIDE_FIELD_LEAK = re.compile(r"source_status|verification_status|source_quote|plan_key|item_index|target_evidence")
_UNSUPPORTED_PAST_CLAIM = re.compile(
    r"(?:我|本人)(?:(?:当时|曾经|已经|已)){0,2}(?:完成|做过|负责|实施|主导|搭建|实现|提升|优化|验证|交付|推进)"
)


def _has_unsupported_past_claim(text: str) -> bool:
    """Catch explicit first-person accomplishments, not verify their truth."""
    compact = re.sub(r"\s+", "", text)
    if re.search(r"(?:我|本人)(?:还)?(?:没有|未|尚未|不曾|不)", compact):
        return False
    if re.search(r"(?:不要(?:说|回答|声称)|避免(?:说|声称)|错误示例|反例|例如|比如).{0,20}[“「『\"']", text):
        # Quoted claims used as warnings/examples are not a recommended answer.
        return False
    if re.match(r"^(?:如果|假设|计划|将|会|希望|准备|拟|目标是)", compact) or re.search(
        r"(?:我|本人)(?:计划|将|会|希望|准备|拟)(?:完成|负责|实施|主导|搭建|提升|优化|验证)", compact
    ):
        return False
    return bool(_UNSUPPORTED_PAST_CLAIM.search(compact))


def unverified_numbers(text: str, jd: str, material: str) -> list[str]:
    """Numbers in resume copy that appear in neither the JD nor the material."""
    haystack = material + "\n" + jd
    return sorted({token for token in re.findall(r"\d+(?:\.\d+)?%?", text) if token not in haystack})


def gap_phrases(text: str) -> list[str]:
    """Gap commentary ('尚未具备…') belongs in guides, never in job-version copy."""
    return [phrase for phrase in GAP_PHRASES if phrase in text]


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
    dropped_source_quotes = 0
    for kind in ("job", "blueprint"):
        resume = getattr(draft, kind)
        items = []
        for index, item in enumerate(resume.items):
            if item.plan_key not in plan_keys:
                raise ValueError("unknown plan")
            flags: list[str] = []
            quote = item.source_quote.strip()
            if quote and quote not in material:
                # Model paraphrases are not evidence. Keep the text as AI-authored,
                # discard the invalid citation, and count it: a fabricated citation
                # is a quality signal, not something to silently absorb.
                quote = ""
                flags.append("dropped_source_quote")
                dropped_source_quotes += 1
            source_status = "user_confirmed" if confirmed and quote else "user_unconfirmed" if quote else "none"
            text = item.text.strip()
            # A confirmed quote grounds only that quote, not any model expansion.
            # Automatic completion requires the whole job item to repeat confirmed
            # source text verbatim. Blueprint items stay unfinished until the user
            # explicitly confirms them later, regardless of model completion claims.
            achieved = bool(kind == "job" and item.achieved and confirmed and quote and text == quote)
            if item.achieved and not achieved:
                flags.append("achievement_unverified")
            items.append({
                "id": f"{kind}-{index + 1}",
                "plan_key": item.plan_key,
                "text": text,
                "source_quote": quote,
                "source_status": source_status,
                "verification_status": "ai_generated",
                "achieved": achieved,
                "flags": flags,
            })
        resumes[kind] = {"title": resume.title.strip(), "summary": resume.summary.strip(), "items": items}
    # Version-specific IDs are not visible copy and must not make identical
    # resumes appear different.
    def visible_resume(resume: dict) -> dict:
        return {"summary": resume["summary"], "items": [item["text"] for item in resume["items"]]}
    if stable_hash(visible_resume(resumes["job"])) == stable_hash(visible_resume(resumes["blueprint"])):
        raise ValueError("dual resumes identical")
    flagged_items = sum(1 for kind in resumes for item in resumes[kind]["items"] if item["flags"])
    return {"plans": plans, "resumes": resumes, "quality": {"dropped_source_quotes": dropped_source_quotes, "flagged_items": flagged_items}}


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
        for field in (entry.question, entry.answer_outline, entry.contribution_boundary, entry.verification, entry.practice_step):
            if _GUIDE_FIELD_LEAK.search(field):
                raise ValueError("internal field leaked into guide")
        if kind == "blueprint" and not item["achieved"] and not entry.practice_step.strip():
            raise ValueError("unfinished blueprint missing practice")
        if kind == "blueprint" and not item["achieved"] and re.search(r"请介绍一次|你(?:曾经|已经|当时).{0,12}(?:如何|完成|做过)|讲讲你做过", entry.question):
            raise ValueError("unfinished blueprint question implies completed experience")
        if kind == "blueprint" and not item["achieved"] and _has_unsupported_past_claim(entry.answer_outline):
            raise ValueError("unfinished blueprint answer claims unverified past experience")
        result.append({"item_id": item["id"], **entry.model_dump(exclude={"item_index"})})
    return result
