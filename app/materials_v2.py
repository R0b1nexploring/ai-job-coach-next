"""Explicitly reusable personal materials and safe batch snapshot binding.

All operations use the personal installation's deletion-journal guard.  This
module never uploads original files or automatically calls a model.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping
from types import MappingProxyType

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.content_v2 import Claim, ClaimEvidence, normalize_material_text, text_hash
from app.facts_v2 import create_fact_snapshot, create_material_revision
from app.models import MaterialRevision
from app.privacy_v2 import V2Deleted, require_material_guard, require_v2_guard, v2_write_transaction


def _metadata(row: MaterialRevision) -> dict:
    from datetime import timezone
    created = row.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return {"material_id": row.material_id, "revision_id": row.id, "revision": row.revision,
            "source_type": row.source_type, "content_hash": row.content_hash,
            "created_at": created.isoformat()}


def _validated_source(session: Session, *, revision_id: str, material_id: str | None = None) -> MaterialRevision:
    require_v2_guard(session, scope_id="local")
    row = session.get(MaterialRevision, revision_id)
    if row is None or (material_id is not None and row.material_id != material_id):
        raise V2Deleted("material unavailable")
    require_material_guard(session, scope_id="local", material_id=row.material_id, revision_id=row.id)
    if row.retention != "independent" or row.owner_batch_id is not None:
        raise V2Deleted("material unavailable")
    if row.extracted_text != normalize_material_text(row.extracted_text) or text_hash(row.extracted_text) != row.content_hash:
        raise ValueError("素材校验失败")
    return row


def create_independent_material(session_factory, *, extracted_text: str, source_type: str,
                                material_id: str | None = None) -> dict:
    if not isinstance(extracted_text, str) or not extracted_text.strip() or len(extracted_text) > 20_000:
        raise ValueError("素材正文无效")
    with v2_write_transaction(session_factory) as session:
        if material_id is not None:
            require_material_guard(session, material_id=material_id)
        row = create_material_revision(session, scope_id="local", source_type=source_type,
                                       extracted_text=extracted_text, material_id=material_id,
                                       retain_independently=True)
        return _metadata(row)


def list_independent_materials(session_factory) -> list[dict]:
    with v2_write_transaction(session_factory) as session:
        guard = session.info["v2_journal_guard"]
        rows = session.scalars(select(MaterialRevision).where(MaterialRevision.scope_id == "local",
            MaterialRevision.retention == "independent", MaterialRevision.owner_batch_id.is_(None))
            .order_by(MaterialRevision.material_id, MaterialRevision.revision.desc())).all()
        latest = {}
        for row in rows:
            if row.material_id not in guard["deleted_material_ids"] and row.id not in guard["deleted_revision_ids"]:
                latest.setdefault(row.material_id, _metadata(row))
        return sorted(latest.values(), key=lambda item: (item["created_at"], item["material_id"]), reverse=True)


def list_material_revisions(session_factory, *, material_id: str) -> list[dict]:
    with v2_write_transaction(session_factory) as session:
        require_material_guard(session, material_id=material_id)
        rows = session.scalars(select(MaterialRevision).where(MaterialRevision.material_id == material_id)
                               .order_by(MaterialRevision.revision.desc())).all()
        if any(row.scope_id != "local" or row.retention != "independent" or row.owner_batch_id is not None for row in rows):
            raise V2Deleted("material unavailable")
        return [_metadata(row) for row in rows]


def read_material_revision(session_factory, *, material_id: str, revision_id: str) -> dict:
    with v2_write_transaction(session_factory) as session:
        row = _validated_source(session, revision_id=revision_id, material_id=material_id)
        return {**_metadata(row), "extracted_text": row.extracted_text}


@dataclass(frozen=True)
class ReusedMaterialSnapshot:
    material_revision_id: str
    fact_snapshot_id: str
    claim_ids: Mapping[str, str]


def freeze_reused_material(session: Session, *, revision_id: str, candidates: list[dict]) -> ReusedMaterialSnapshot:
    """Freeze source-validated claims in a caller-owned new-batch transaction.

    The caller must bind the returned snapshot and material revision to the new
    batch with bind_batch_snapshot before this same guard commits. Only explicit
    candidate.confirmed=True produces a user-confirmed claim. No material text is
    sent to a provider by this helper.
    """
    require_v2_guard(session, scope_id="local")
    if not session.info["v2_journal_guard"]["batch_id"]:
        raise ValueError("必须在批次事务内冻结素材")
    if not isinstance(candidates, list) or len(candidates) > 200:
        raise ValueError("事实候选无效")
    source = _validated_source(session, revision_id=revision_id)
    claims, confirmed = [], set()
    for index, item in enumerate(candidates):
        if not isinstance(item, dict) or type(item.get("confirmed", False)) is not bool:
            raise ValueError("事实确认状态无效")
        try:
            if (not isinstance(item["fact_type"], str) or not 1 <= len(item["fact_type"]) <= 80
                or not isinstance(item["value"], str) or len(item["value"]) > 2000):
                raise ValueError("invalid fact")
            # Python bool is an int; reject it and string coercion for offsets.
            if type(item["start"]) is not int or type(item["end"]) is not int:
                raise ValueError("invalid offset")
            evidence = ClaimEvidence(source_revision_id=source.id, start=item["start"], end=item["end"], quote_hash=item["quote_hash"])
            key = f"c{index + 1}"
            state = "user_confirmed" if item.get("confirmed", False) else "unconfirmed"
            claims.append(Claim(claim_id=key, fact_type=item["fact_type"], value=item["value"],
                                evidence=(evidence,), confirmation=state, completion=item.get("completion", "unknown")))
            if state == "user_confirmed":
                confirmed.add(key)
        except (KeyError, TypeError, ValueError):
            raise ValueError("事实来源或内容无效") from None
    result = create_fact_snapshot(session, scope_id="local", claims=claims, user_confirmed_claim_keys=frozenset(confirmed))
    return ReusedMaterialSnapshot(source.id, result.snapshot.id, MappingProxyType(dict(result.claim_ids)))
