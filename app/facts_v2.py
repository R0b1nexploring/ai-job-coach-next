"""Internal S1 persistence primitives, not an API or a deletion implementation.

Call inside a caller-owned transaction.  No original upload or diagnostic text
is stored.  V2 data must not be exposed to users until deletion/recovery is
integrated for these records and the surrounding batch workflow.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.content_v2 import (
    Claim, FactSnapshot as FactContract, MaterialRevision as MaterialContract,
    normalize_material_text, text_hash, validate_snapshot_evidence,
)
from app.models import FactSnapshot, MaterialRevision, new_id, utcnow


@dataclass(frozen=True)
class CreatedFactSnapshot:
    snapshot: FactSnapshot
    claim_ids: Mapping[str, str]


class FactStorageError(ValueError):
    """A non-sensitive storage/validation failure."""


def _material_contract(row: MaterialRevision) -> MaterialContract:
    # SQLite drops timezone information; stored times are UTC in this app.
    from datetime import timezone
    created = row.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return MaterialContract(
        revision_id=row.id, private_scope=row.scope_id, source_type=row.source_type,
        extracted_text=row.extracted_text, content_hash=row.content_hash, created_at=created,
    )


def create_material_revision(
    session: Session, *, scope_id: str, source_type: str, extracted_text: str,
    material_id: str | None = None, retain_independently: bool = False,
) -> MaterialRevision:
    """Insert one new immutable material version; never update an old version.

    The unique material/revision constraint arbitrates concurrent edits; on a
    conflict the caller must roll back and retry in a new transaction.
    """
    from app.privacy_v2 import DeletionPlanError, require_v2_guard
    try:
        require_v2_guard(session, scope_id=scope_id)
    except DeletionPlanError:
        raise FactStorageError("v2 journal transaction required") from None
    if not scope_id or len(scope_id) > 64 or any(c.isspace() for c in scope_id):
        raise FactStorageError("invalid scope")
    guard_batch = session.info["v2_journal_guard"]["batch_id"]
    if type(retain_independently) is not bool:
        raise FactStorageError("invalid retention choice")
    retention = "independent" if retain_independently else "batch"
    owner = None if retain_independently else guard_batch
    if retention == "batch" and not owner:
        raise FactStorageError("batch-owned material requires a batch transaction")
    is_new = material_id is None
    if is_new:
        material_id, number = new_id(), 1
    if material_id in session.info["v2_journal_guard"]["deleted_material_ids"]:
        raise FactStorageError("material is unavailable")
    if not is_new:
        versions = session.scalars(
            select(MaterialRevision).where(MaterialRevision.material_id == material_id)
            .order_by(MaterialRevision.revision.desc())
        ).all()
        if not versions or any(row.scope_id != scope_id or row.retention != retention or row.owner_batch_id != owner for row in versions):
            raise FactStorageError("material is unavailable")
        number = versions[0].revision + 1
    text = normalize_material_text(extracted_text)
    now = utcnow()
    revision_id = new_id()
    # Validate before adding anything to the session; the contract verifies hash,
    # newline form and source type.  Original filenames are not persisted here.
    try:
        contract = MaterialContract(
            revision_id=revision_id, private_scope=scope_id, source_type=source_type,
            extracted_text=text, content_hash=text_hash(text), created_at=now,
        )
    except (ValueError, TypeError):
        raise FactStorageError("invalid material") from None
    row = MaterialRevision(id=contract.revision_id, material_id=material_id, revision=number,
                           scope_id=scope_id, source_type=contract.source_type,
                           extracted_text=contract.extracted_text, content_hash=contract.content_hash,
                           retention=retention, owner_batch_id=owner, created_at=now)
    session.add(row)
    session.flush()
    return row


def create_fact_snapshot(
    session: Session, *, scope_id: str, claims: Sequence[Claim],
    user_confirmed_claim_keys: frozenset[str] = frozenset(),
) -> CreatedFactSnapshot:
    """Freeze source-validated claims and private evidence copies.

    Input claim IDs are temporary keys and become application-issued IDs.  A
    confirmed value requires the caller to identify the explicit user action;
    extraction alone must never pass that authorization. The caller owns commit.
    The returned mapping is transient; only application-issued IDs are stored.
    """
    from app.privacy_v2 import DeletionPlanError, require_v2_guard
    try:
        require_v2_guard(session, scope_id=scope_id)
    except DeletionPlanError:
        raise FactStorageError("v2 journal transaction required") from None
    if not scope_id or len(scope_id) > 64 or any(c.isspace() for c in scope_id):
        raise FactStorageError("invalid scope")
    try:
        claims = tuple(Claim.model_validate(claim.model_dump()) for claim in claims)
    except (AttributeError, ValueError, TypeError):
        raise FactStorageError("invalid claims") from None
    keys = [claim.claim_id for claim in claims]
    if len(set(keys)) != len(keys) or not user_confirmed_claim_keys <= set(keys):
        raise FactStorageError("invalid claim keys")
    if any(c.confirmation == "user_confirmed" and c.claim_id not in user_confirmed_claim_keys for c in claims):
        raise FactStorageError("confirmation requires explicit user action")
    if any(c.confirmation != "user_confirmed" and c.claim_id in user_confirmed_claim_keys for c in claims):
        raise FactStorageError("invalid confirmation action")
    mapping = {key: new_id() for key in keys}
    try:
        frozen_claims = tuple(
            claim.model_copy(update={"claim_id": mapping[claim.claim_id],
                                     "conflict_ids": tuple(mapping[key] for key in claim.conflict_ids)})
            for claim in claims
        )
        snapshot = FactContract(snapshot_id=new_id(), private_scope=scope_id, claims=frozen_claims)
        source_ids = sorted({e.source_revision_id for claim in snapshot.claims for e in claim.evidence})
        if set(source_ids).intersection(session.info["v2_journal_guard"]["deleted_revision_ids"]):
            raise ValueError("source was deleted")
        rows = session.scalars(select(MaterialRevision).where(MaterialRevision.id.in_(source_ids))).all() if source_ids else []
        if any(row.material_id in session.info["v2_journal_guard"]["deleted_material_ids"] for row in rows):
            raise ValueError("source was deleted")
        guard_batch = session.info["v2_journal_guard"]["batch_id"]
        if any(not ((row.retention == "independent" and row.owner_batch_id is None) or
                    (row.retention == "batch" and guard_batch and row.owner_batch_id == guard_batch))
               for row in rows):
            raise ValueError("material ownership unavailable")
        sources = tuple(_material_contract(row) for row in rows)
        validate_snapshot_evidence(snapshot, sources)
    except (KeyError, ValueError, TypeError):
        raise FactStorageError("invalid facts or source evidence") from None
    # Copy the exact normalized source, enabling historical verification after a
    # future supported source-retirement operation.  Never copy filenames.
    copies = [source.model_dump(mode="json") for source in sorted(sources, key=lambda item: item.revision_id)]
    row = FactSnapshot(id=snapshot.snapshot_id, scope_id=scope_id,
                       claims=[claim.model_dump(mode="json") for claim in snapshot.claims],
                       source_copies=copies, created_at=utcnow())
    session.add(row)
    session.flush()
    return CreatedFactSnapshot(snapshot=row, claim_ids=MappingProxyType(mapping))
