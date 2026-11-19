"""Prepare an explicit v1-to-v2 *new revision* without mutating history.

This module performs no writes, publishes nothing, and makes no authenticity or
quality claim. Callers must load an explicitly selected immutable v1 revision
and its own plan snapshot, then atomically save a new v2 revision and this
module's provenance under the installation's deletion and concurrency guards.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from typing import Literal
from uuid import UUID, uuid4

from app.content_v2 import FactSnapshot, ResumeDocumentV2, validate_document_references


@dataclass(frozen=True)
class V1UpgradeDraft:
    document: dict
    plans: list[dict]
    header: dict
    fact_snapshot: FactSnapshot
    provenance: dict
    id_map: dict


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _valid_uuid(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 32:
        return False
    try:
        parsed = UUID(hex=value)
    except ValueError:
        return False
    return parsed.version == 4 and parsed.hex == value


def prepare_v1_upgrade(*, legacy_resume: dict, legacy_plans: list[dict],
                       source_resume_id: str, source_revision: int, private_scope: str,
                       kind: Literal["job", "blueprint"], locale: Literal["zh-CN", "en"] = "zh-CN",
                       id_map: dict | None = None) -> V1UpgradeDraft:
    """Return a reviewable migration candidate, retaining all old data untouched.

    `id_map` may be reused only with identical source revision and inputs.  The
    caller must persist it together with the new revision if retry is supported.
    Evidence is not guessed from v1 quotes: the original source and verification
    statuses remain in provenance, while new facts stay unconfirmed and empty.
    """
    if not isinstance(legacy_resume, dict) or not isinstance(legacy_plans, list):
        raise ValueError("invalid legacy revision")
    if not isinstance(source_resume_id, str) or not source_resume_id.strip() or not isinstance(source_revision, int) or isinstance(source_revision, bool) or source_revision < 1:
        raise ValueError("invalid legacy revision identity")
    if not isinstance(private_scope, str) or not private_scope.strip() or any(c.isspace() for c in private_scope):
        raise ValueError("invalid private scope")
    if kind not in {"job", "blueprint"} or locale not in {"zh-CN", "en"}:
        raise ValueError("invalid document selection")
    if legacy_resume.get("schema_version") not in (None, 1) or "sections" in legacy_resume:
        raise ValueError("expected a v1 revision")
    if legacy_resume.get("status") not in (None, "ready"):
        raise ValueError("legacy draft is not ready for explicit editing")
    title, summary, items = legacy_resume.get("title"), legacy_resume.get("summary"), legacy_resume.get("items")
    if not isinstance(title, str) or not title.strip() or not isinstance(summary, str) or not summary.strip() or not isinstance(items, list):
        raise ValueError("legacy revision is incomplete")
    header = legacy_resume.get("header") or {}
    if not isinstance(header, dict):
        raise ValueError("invalid legacy header")
    plan_by_key = {}
    for plan in legacy_plans:
        if not isinstance(plan, dict) or not isinstance(plan.get("key"), str) or not plan["key"] or plan["key"] in plan_by_key:
            raise ValueError("invalid legacy plan snapshot")
        plan_by_key[plan["key"]] = plan
    item_ids = set()
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip():
            raise ValueError("legacy item is incomplete")
        if item.get("plan_key") not in plan_by_key:
            raise ValueError("legacy item has no bound plan snapshot")
        if item.get("achieved") is not None and type(item["achieved"]) is not bool:
            raise ValueError("invalid legacy completion")
        if "locked" in item and type(item["locked"]) is not bool:
            raise ValueError("invalid legacy lock")
        item_id = item.get("id")
        if item_id is not None:
            if not isinstance(item_id, str) or not item_id or item_id in item_ids:
                raise ValueError("invalid legacy item ID")
            item_ids.add(item_id)
    source_hash = _digest({"source_resume_id": source_resume_id, "source_revision": source_revision,
                           "scope": private_scope, "kind": kind, "locale": locale,
                           "resume": legacy_resume, "plans": legacy_plans})
    required = {"snapshot", "summary"}
    if items:
        required.add("section:projects")
    for key in plan_by_key:
        required.add("plan:" + key)
    for index in range(len(items)):
        required.update({f"entry:{index}", f"unit:{index}"})
    if id_map is None:
        ids = {key: uuid4().hex for key in sorted(required)}
    else:
        if not isinstance(id_map, dict) or id_map.get("source_hash") != source_hash or not isinstance(id_map.get("ids"), dict):
            raise ValueError("ID map does not match legacy revision")
        ids = deepcopy(id_map["ids"])
        if set(ids) != required or len(set(ids.values())) != len(ids) or any(not _valid_uuid(value) for value in ids.values()):
            raise ValueError("invalid ID map")
    plans = []
    for legacy_plan in legacy_plans:
        plan = deepcopy(legacy_plan)
        plan["key"] = ids["plan:" + legacy_plan["key"]]
        plans.append(plan)
    entries, provenance_items = [], []
    for index, item in enumerate(items):
        plan_key = item["plan_key"]
        completion = "completed" if item.get("achieved") is True else "unknown"
        unit = {"unit_id": ids[f"unit:{index}"], "text": item["text"], "claim_refs": [], "support": [],
                "authorship": "ai_proposed", "completion": completion, "purpose": "experience",
                "plan_ref": ids["plan:" + plan_key], "locked": item.get("locked", False)}
        entries.append({"entry_id": ids[f"entry:{index}"], "name": plan_by_key[plan_key].get("title"),
                        "plan_ref": ids["plan:" + plan_key], "units": [unit]})
        quote = item.get("source_quote")
        provenance_items.append({"legacy_item_id": item.get("id"), "new_unit_id": unit["unit_id"],
                                 "legacy_plan_key": plan_key, "legacy_achieved": item.get("achieved"),
                                 "source_status": item.get("source_status"),
                                 "verification_status": item.get("verification_status"),
                                 "source_quote_hash": _digest(quote) if isinstance(quote, str) and quote else None})
    document = ResumeDocumentV2.model_validate({"kind": kind, "locale": locale, "title": title,
        "summary": {"unit_id": ids["summary"], "text": summary, "claim_refs": [], "support": [],
                    "authorship": "ai_proposed", "completion": "unknown", "purpose": "summary"},
        "sections": [{"section_id": ids["section:projects"], "type": "projects", "entries": entries}] if entries else []})
    facts = FactSnapshot(snapshot_id=ids["snapshot"], private_scope=private_scope, claims=())
    validate_document_references(document, facts, {plan["key"] for plan in plans})
    provenance = {"migration_version": 1, "source_resume_id": source_resume_id,
                  "source_revision": source_revision, "source_hash": source_hash,
                  "legacy_schema_version": 1, "items": provenance_items}
    return V1UpgradeDraft(document=document.model_dump(mode="json"), plans=plans, header=deepcopy(header),
                          fact_snapshot=facts, provenance=provenance,
                          id_map={"source_hash": source_hash, "ids": deepcopy(ids)})
