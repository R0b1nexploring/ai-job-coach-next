"""Versioned v2 document fingerprints and guide coverage validation.

These helpers operate on frozen, already validated snapshots. They do not
create guides, publish revisions, or infer that any claim is true.
"""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy


def _hash(kind: str, payload: object) -> str:
    encoded = json.dumps({"algorithm": kind, "payload": payload}, ensure_ascii=False,
                         sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    # The schema version identifies the algorithm for persisted v2 hashes;
    # hashing the algorithm label also separates domains. Keep the digest at
    # 64 characters so existing hash columns can hold a v2 revision.
    return hashlib.sha256(encoded).hexdigest()


def content_hash(document: dict, header: dict, plans: object) -> str:
    """Identify the complete saved document, including private display fields."""
    return _hash("content-v2", {"document": document, "header": header, "plans": plans})


def semantic_hash(document: dict, target: object, facts: object, plans: object) -> str:
    """Fingerprint frozen guide inputs; callers supply non-contact facts only.

    The header is intentionally not an argument. Lock state affects editing,
    not what the guide needs to explain. Order and all other document fields
    remain significant.
    """
    semantic_document = deepcopy(document)
    semantic_document.get("summary", {}).pop("locked", None)
    for section in semantic_document.get("sections", []):
        for entry in section.get("entries", []):
            for unit in entry.get("units", []):
                unit.pop("locked", None)
    return _hash("semantic-v2", {"document": semantic_document, "target": target,
                                 "facts": facts, "plans": plans})


def guide_input_hash(semantic: str, locale: str, coverage: list[str], strategy: str) -> str:
    if locale not in {"zh-CN", "en"}:
        raise ValueError("unsupported guide locale")
    if len(coverage) != len(set(coverage)):
        raise ValueError("duplicate coverage unit")
    return _hash("guide-input-v2", {"semantic_hash": semantic, "locale": locale,
                                    "coverage": sorted(coverage), "strategy": strategy})


_EMPTY_JOB_SUMMARY_BY_LOCALE = {
    "zh-CN": "请填写个人背景与求职目标。",
    "en": "Add your background and career goal.",
}


def _is_fixed_empty_job_summary(document: dict, summary: dict) -> bool:
    """Recognize only the application's exact, unreferenced job placeholder.

    Every other summary remains in scope, including unreferenced claims and
    user-authored or user-edited text. Never infer factual absence from lack
    of citations alone.
    """
    return (
        document.get("kind") == "job"
        and summary.get("authorship") == "ai_proposed"
        and not summary.get("claim_refs")
        and summary.get("text") == _EMPTY_JOB_SUMMARY_BY_LOCALE.get(document.get("locale"))
    )


def coverage_units(document: dict) -> list[str]:
    """Return the ordered set of units needing an interview explanation.

    Substantive summaries and skill units remain in scope conservatively:
    sharing claim references alone does not prove other wording covers them.
    Only the exact application-owned empty-job placeholder is excluded.
    """
    main: list[dict] = []
    secondary: list[dict] = []
    summary = document.get("summary")
    if summary and summary.get("text", "").strip() and not _is_fixed_empty_job_summary(document, summary):
        secondary.append(summary)
    for section in document.get("sections", []):
        section_type = section.get("type")
        for entry in section.get("entries", []):
            for unit in entry.get("units", []):
                if not unit.get("text", "").strip():
                    continue
                if (section_type in {"experience", "projects"}
                        or unit.get("completion") in {"planned", "in_progress"}
                        or (section_type == "education" and unit.get("purpose") in {"experience", "proposal"})):
                    main.append(unit)
                elif section_type == "skills":
                    secondary.append(unit)
    selected = main + secondary
    ids = [unit["unit_id"] for unit in selected]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate unit id")
    return ids


def validate_guide_coverage(entries: list[dict], context_notes: list[dict], coverage: list[str]) -> None:
    """Reject missing, dangling or duplicated guide answers.

    This is structural validation only; it cannot establish semantic relevance.
    """
    required = set(coverage)
    if len(required) != len(coverage):
        raise ValueError("duplicate coverage unit")
    seen: set[str] = set()
    answers: set[tuple[str, str]] = set()
    for entry in entries:
        refs = entry.get("unit_ids")
        if not isinstance(refs, list) or not refs or len(refs) != len(set(refs)) or not set(refs) <= required:
            raise ValueError("invalid guide unit references")
        question = entry.get("question", "").strip()
        answer = entry.get("answer_outline", "").strip()
        if not question or not answer or (question, answer) in answers:
            raise ValueError("empty or duplicate guide answer")
        answers.add((question, answer))
        seen.update(refs)
    if seen != required:
        raise ValueError("guide coverage incomplete")
    if not required and (not context_notes or not any(str(note.get("text", "")).strip() for note in context_notes)):
        raise ValueError("empty coverage requires context notes")
