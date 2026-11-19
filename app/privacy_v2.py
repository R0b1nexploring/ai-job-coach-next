"""Personal-install v2 deletion primitives and journal transaction guard.

Correctness requires every v2 reader, writer and reference creator to hold the
same journal guard. This is not a multi-host lock or a public API.
"""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Batch, FactSnapshot, MaterialRevision

ResourceId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
ScopeId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^\S+$")]


class DeletionPlanError(ValueError):
    """The intended deletion cannot be safely frozen or replayed."""


class DeletionImpactChanged(DeletionPlanError):
    """The preview no longer matches the graph under the lock."""


class V2Deleted(DeletionPlanError):
    """The requested batch has a durable deletion intent."""


class BatchDeletionIntent(BaseModel):
    """Names the exact dependency graph at intent time, not personal content."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[2] = 2
    resource_type: Literal["batch"] = "batch"
    scope_id: ScopeId
    resource_id: ResourceId
    fact_snapshot_ids: tuple[ResourceId, ...]
    owned_material_revision_ids: tuple[ResourceId, ...] = ()

    @model_validator(mode="after")
    def unique_snapshots(self):
        if len(set(self.owned_material_revision_ids)) != len(self.owned_material_revision_ids):
            raise ValueError("duplicate material revision")
        if len(self.fact_snapshot_ids) != 1:
            raise ValueError("exactly one batch-owned fact snapshot is required")
        return self


class MaterialDeletionIntent(BaseModel):
    """Frozen list of dependent batches at material-deletion decision time."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    version: Literal[2] = 2
    resource_type: Literal["material"] = "material"
    scope_id: ScopeId
    resource_id: ResourceId
    revision_ids: tuple[ResourceId, ...]
    mode: Literal["cascade", "preserve_existing"]
    affected_batches: tuple[BatchDeletionIntent, ...]

    @model_validator(mode="after")
    def validate_graph(self):
        if not self.revision_ids or len(set(self.revision_ids)) != len(self.revision_ids):
            raise ValueError("invalid material revisions")
        batch_ids = [item.resource_id for item in self.affected_batches]
        if len(set(batch_ids)) != len(batch_ids) or any(item.scope_id != self.scope_id for item in self.affected_batches):
            raise ValueError("invalid affected batches")
        return self


def _other_references(session: Session, snapshot_id: str, batch_id: str) -> bool:
    return session.scalar(select(Batch.id).where(
        Batch.fact_snapshot_id == snapshot_id, Batch.id != batch_id,
    ).limit(1)) is not None


def _validate_owned_material_graph(session: Session, intent: BatchDeletionIntent) -> None:
    frozen = set(intent.owned_material_revision_ids)
    # A newly discovered owned revision means this record cannot establish the
    # full deletion graph; recovery must stay closed instead of leaking it.
    owned = session.scalars(select(MaterialRevision).where(
        MaterialRevision.owner_batch_id == intent.resource_id)).all()
    if any(row.scope_id != intent.scope_id or row.retention != "batch" or row.id not in frozen for row in owned):
        raise DeletionPlanError("owned material differs from frozen intent")
    for row in session.scalars(select(MaterialRevision).where(MaterialRevision.id.in_(frozen))).all() if frozen else []:
        if row.scope_id != intent.scope_id or row.retention != "batch" or row.owner_batch_id != intent.resource_id:
            raise DeletionPlanError("material ownership differs from frozen intent")
    for batch in session.scalars(select(Batch).where(Batch.id != intent.resource_id)).all():
        if batch.material_revision_ids is not None and (not isinstance(batch.material_revision_ids, list) or any(not isinstance(value, str) for value in batch.material_revision_ids)):
            raise DeletionPlanError("invalid material references")
        if frozen.intersection(batch.material_revision_ids or []):
            raise DeletionPlanError("owned material has another batch reference")
    for snapshot in session.scalars(select(FactSnapshot)).all():
        if snapshot.id not in intent.fact_snapshot_ids and any(
            source.get("revision_id") in frozen for source in snapshot.source_copies
        ):
            raise DeletionPlanError("owned material has another snapshot reference")


def freeze_batch_intent(session: Session, *, scope_id: str, batch_id: str, retired_revision_ids: frozenset[str] = frozenset()) -> BatchDeletionIntent:
    """Prepare an intent without changing the database or durable journal.

    Requires a v2 batch with its private fact snapshot.  Explicitly saved
    materials are intentionally absent.  The caller must not use this outside an
    established reference/deletion lock protocol, which is not implemented yet.
    """
    batch = session.scalar(select(Batch).where(Batch.id == batch_id).with_for_update())
    if batch is None or batch.schema_version != 2 or not batch.fact_snapshot_id:
        raise DeletionPlanError("batch or private snapshot is unavailable")
    snapshot = session.scalar(select(FactSnapshot).where(
        FactSnapshot.id == batch.fact_snapshot_id).with_for_update())
    if snapshot is None or snapshot.scope_id != scope_id:
        raise DeletionPlanError("batch or private snapshot is unavailable")
    if _other_references(session, snapshot.id, batch.id):
        raise DeletionPlanError("snapshot is shared")
    if (not isinstance(batch.material_revision_ids, list)
        or any(not isinstance(value, str) for value in batch.material_revision_ids)
        or len(set(batch.material_revision_ids)) != len(batch.material_revision_ids)):
        raise DeletionPlanError("batch material references unavailable")
    selected = set(batch.material_revision_ids)
    if (not isinstance(snapshot.source_copies, list)
        or any(not isinstance(source, dict) or source.get("revision_id") not in selected for source in snapshot.source_copies)):
        raise DeletionPlanError("snapshot source references unavailable")
    material_rows = session.scalars(select(MaterialRevision).where(MaterialRevision.id.in_(selected))).all() if selected else []
    if (selected - {row.id for row in material_rows}) - retired_revision_ids or any(
        row.scope_id != scope_id or not (
            (row.retention == "batch" and row.owner_batch_id == batch.id) or
            (row.retention == "independent" and row.owner_batch_id is None)
        ) for row in material_rows
    ):
        raise DeletionPlanError("material ownership is unavailable")
    owned = session.scalars(select(MaterialRevision).where(MaterialRevision.owner_batch_id == batch.id)).all()
    try:
        intent = BatchDeletionIntent(scope_id=scope_id, resource_id=batch.id, fact_snapshot_ids=(snapshot.id,),
                                     owned_material_revision_ids=tuple(sorted(row.id for row in owned)))
        _validate_owned_material_graph(session, intent)
        return intent
    except ValueError:
        raise DeletionPlanError("invalid intent identifiers") from None


def validate_replay(session: Session, intent: BatchDeletionIntent) -> tuple[str, ...]:
    """Validate a frozen intent against a restored DB; perform no deletion.

    Missing rows are harmless for an idempotent replay, but mismatched scope,
    changed dependencies and newly shared snapshots stop restoration.  Actual
    application remains blocked until journal versioning and locking are wired.
    """
    intent = BatchDeletionIntent.model_validate(intent.model_dump())
    batch = session.scalar(select(Batch).where(Batch.id == intent.resource_id).with_for_update())
    if batch is not None and (batch.schema_version != 2 or batch.fact_snapshot_id not in intent.fact_snapshot_ids):
        raise DeletionPlanError("restored batch differs from frozen intent")
    for snapshot_id in intent.fact_snapshot_ids:
        snapshot = session.scalar(select(FactSnapshot).where(FactSnapshot.id == snapshot_id).with_for_update())
        if snapshot is not None and snapshot.scope_id != intent.scope_id:
            raise DeletionPlanError("restored snapshot scope differs from frozen intent")
        if _other_references(session, snapshot_id, intent.resource_id):
            raise DeletionPlanError("restored snapshot is shared")
    _validate_owned_material_graph(session, intent)
    return intent.fact_snapshot_ids


from contextlib import contextmanager
import json
import os


@contextmanager
def v2_write_transaction(session_factory, *, scope_id: str = "local", batch_id: str | None = None):
    """Hold the independent journal lock across the entire local DB transaction.

    All v2 creators, readers, workers and writers must use this same guard;
    supply the exact batch ID for every operation involving a batch, including
    creation. Calling other journal-locking functions while inside would deadlock.
    This protocol is for one personal installation on a shared local filesystem,
    not multi-host or distributed operation.
    """
    from app.privacy import JournalUnavailable, _ids, _journal, _records, installation_id

    if scope_id != "local":
        raise DeletionPlanError("unsupported private scope")
    if batch_id is not None:
        try:
            BatchDeletionIntent(scope_id=scope_id, resource_id=batch_id, fact_snapshot_ids=("0" * 32,))
        except ValueError:
            raise DeletionPlanError("invalid batch identifier") from None
    installation_id()
    with _journal() as stream:
        records = _records(stream)
        deleted = _ids(stream)
        materials = [record for record in records if record.get("resource_type") == "material"]
        deleted_material_ids = {record["resource_id"] for record in materials}
        deleted_revisions = {identifier for record in materials for identifier in record["revision_ids"]}
        if batch_id in deleted:
            raise V2Deleted("batch was deleted")
        with session_factory() as session:
            session.info["v2_journal_guard"] = {"scope_id": scope_id, "batch_id": batch_id, "deleted_ids": deleted, "deleted_material_ids": deleted_material_ids, "deleted_revision_ids": deleted_revisions}
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise
            finally:
                session.info.pop("v2_journal_guard", None)


def require_v2_guard(session: Session, *, scope_id: str, batch_id: str | None = None) -> None:
    guard = session.info.get("v2_journal_guard")
    if not guard or guard.get("scope_id") != scope_id or (batch_id is not None and guard.get("batch_id") != batch_id):
        raise DeletionPlanError("v2 journal transaction required")
    if batch_id is not None and batch_id in guard["deleted_ids"]:
        raise V2Deleted("batch was deleted")


def bind_batch_snapshot(session: Session, *, batch_id: str, snapshot_id: str,
                        material_revision_ids: tuple[str, ...], scope_id: str = "local") -> None:
    """Bind exactly one private snapshot, under the same guard as deletion."""
    from app.models import MaterialRevision

    require_v2_guard(session, scope_id=scope_id, batch_id=batch_id)
    batch = session.get(Batch, batch_id)
    snapshot = session.get(FactSnapshot, snapshot_id)
    if batch is None or batch.schema_version != 2 or batch.fact_snapshot_id is not None:
        raise DeletionPlanError("batch cannot accept a fact snapshot")
    if snapshot is None or snapshot.scope_id != scope_id or _other_references(session, snapshot_id, batch_id):
        raise DeletionPlanError("private snapshot is unavailable")
    ids = set(material_revision_ids)
    if ids.intersection(session.info["v2_journal_guard"]["deleted_revision_ids"]):
        raise V2Deleted("material was deleted")
    if len(ids) != len(material_revision_ids):
        raise DeletionPlanError("duplicate material revision")
    rows = session.scalars(select(MaterialRevision).where(MaterialRevision.id.in_(ids))).all() if ids else []
    if len(rows) != len(ids) or any(
        row.scope_id != scope_id or row.material_id in session.info["v2_journal_guard"]["deleted_material_ids"] or not (
            (row.retention == "batch" and row.owner_batch_id == batch_id) or
            (row.retention == "independent" and row.owner_batch_id is None)
        ) for row in rows
    ):
        raise DeletionPlanError("private material is unavailable")
    copied_ids = {item.get("revision_id") for item in snapshot.source_copies}
    if not copied_ids <= ids:
        raise DeletionPlanError("snapshot source is not among selected materials")
    batch.fact_snapshot_id = snapshot_id
    batch.material_revision_ids = sorted(ids)
    session.flush()


def delete_v2_batch(session_factory, *, batch_id: str, scope_id: str = "local") -> None:
    """Durably freeze then delete a v2 batch and its exclusive snapshot.

    Intents survive a failed DB commit. All v2 reads/writes must check the same
    journal under the lock until the operation commits; recovery replays intents.
    """
    from app.privacy import JournalUnavailable, _journal, _records, installation_id

    if scope_id != "local":
        raise DeletionPlanError("unsupported private scope")
    installation_id()
    with _journal() as stream:
        records = _records(stream)
        found = [r for r in records if (r.get("batch_id") if r["version"] == 1 else r.get("resource_id") if r.get("resource_type") == "batch" else None) == batch_id]
        if found and found[0]["version"] != 2:
            raise JournalUnavailable("Conflicting legacy deletion intent")
        with session_factory() as session:
            retired = frozenset(identifier for record in records if record.get("resource_type") == "material" for identifier in record["revision_ids"])
            intent = BatchDeletionIntent.model_validate(found[0]) if found else freeze_batch_intent(session, scope_id=scope_id, batch_id=batch_id, retired_revision_ids=retired)
            if intent.scope_id != scope_id:
                raise JournalUnavailable("Deletion scope mismatch")
            if not found:
                stream.seek(0, os.SEEK_END)
                stream.write(json.dumps(intent.model_dump(mode="json"), separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            snapshot_ids = validate_replay(session, intent)
            batch = session.get(Batch, batch_id)
            if batch:
                session.delete(batch)
            for snapshot_id in snapshot_ids:
                snapshot = session.get(FactSnapshot, snapshot_id)
                if snapshot:
                    session.delete(snapshot)
            for material_id in intent.owned_material_revision_ids:
                material = session.get(MaterialRevision, material_id)
                if material:
                    session.delete(material)
            session.commit()


def freeze_material_intent(session: Session, *, material_id: str, mode: str,
                           scope_id: str = "local", retired_revision_ids: frozenset[str] = frozenset()) -> MaterialDeletionIntent:
    """Freeze independent-material dependencies; caller holds the journal lock."""
    rows = session.scalars(select(MaterialRevision).where(MaterialRevision.material_id == material_id)).all()
    if not rows or any(row.scope_id != scope_id or row.retention != "independent" or row.owner_batch_id is not None for row in rows):
        raise V2Deleted("material unavailable")
    revisions = frozenset(row.id for row in rows)
    all_batches = session.scalars(select(Batch).where(Batch.schema_version == 2)).all()
    all_snapshots = session.scalars(select(FactSnapshot)).all()
    affected: dict[str, Batch] = {}
    for batch in all_batches:
        selected = batch.material_revision_ids
        if not isinstance(selected, list) or any(not isinstance(value, str) for value in selected):
            raise DeletionPlanError("v2 batch material references unavailable")
        if revisions.intersection(selected):
            affected[batch.id] = batch
    for snapshot in all_snapshots:
        if not isinstance(snapshot.source_copies, list) or any(not isinstance(value, dict) for value in snapshot.source_copies):
            raise DeletionPlanError("snapshot sources unavailable")
        if any(source.get("revision_id") in revisions for source in snapshot.source_copies):
            owners = [batch for batch in all_batches if batch.fact_snapshot_id == snapshot.id]
            if len(owners) != 1:
                raise DeletionPlanError("material has an unowned snapshot")
            affected[owners[0].id] = owners[0]
    batch_intents = tuple(freeze_batch_intent(session, scope_id=scope_id, batch_id=batch_id,
                                              retired_revision_ids=retired_revision_ids)
                          for batch_id in sorted(affected))
    try:
        return MaterialDeletionIntent(scope_id=scope_id, resource_id=material_id,
                                      revision_ids=tuple(sorted(revisions)), mode=mode,
                                      affected_batches=batch_intents)
    except ValueError:
        raise DeletionPlanError("invalid material deletion intent") from None


def validate_material_replay(session: Session, intent: MaterialDeletionIntent) -> None:
    """Verify a frozen dependency graph without discovering new delete targets."""
    intent = MaterialDeletionIntent.model_validate(intent.model_dump())
    revisions = set(intent.revision_ids)
    rows = session.scalars(select(MaterialRevision).where(MaterialRevision.material_id == intent.resource_id)).all()
    if any(row.scope_id != intent.scope_id or row.retention != "independent" or row.owner_batch_id is not None or row.id not in revisions for row in rows):
        raise DeletionPlanError("material differs from frozen intent")
    for row in session.scalars(select(MaterialRevision).where(MaterialRevision.id.in_(revisions))).all():
        if row.material_id != intent.resource_id or row.scope_id != intent.scope_id:
            raise DeletionPlanError("material revision differs from frozen intent")
    batch_ids = {batch.resource_id for batch in intent.affected_batches}
    snapshot_ids = {sid for batch in intent.affected_batches for sid in batch.fact_snapshot_ids}
    for batch in session.scalars(select(Batch).where(Batch.schema_version == 2)).all():
        if not isinstance(batch.material_revision_ids, list):
            raise DeletionPlanError("v2 batch material references unavailable")
        if revisions.intersection(batch.material_revision_ids) and batch.id not in batch_ids:
            raise DeletionPlanError("new material reference after intent")
    for snapshot in session.scalars(select(FactSnapshot)).all():
        if not isinstance(snapshot.source_copies, list) or any(not isinstance(value, dict) for value in snapshot.source_copies):
            raise DeletionPlanError("snapshot sources unavailable")
        if any(source.get("revision_id") in revisions for source in snapshot.source_copies) and snapshot.id not in snapshot_ids:
            raise DeletionPlanError("new snapshot reference after intent")
    for batch_intent in intent.affected_batches:
        validate_replay(session, batch_intent)


def apply_material_intent(session: Session, intent: MaterialDeletionIntent) -> None:
    """Apply a validated frozen intent inside caller's locked transaction."""
    validate_material_replay(session, intent)
    if intent.mode == "cascade":
        for item in intent.affected_batches:
            batch = session.get(Batch, item.resource_id)
            if batch:
                session.delete(batch)
            for sid in item.fact_snapshot_ids:
                snapshot = session.get(FactSnapshot, sid)
                if snapshot:
                    session.delete(snapshot)
            for rid in item.owned_material_revision_ids:
                material = session.get(MaterialRevision, rid)
                if material:
                    session.delete(material)
    for rid in intent.revision_ids:
        material = session.get(MaterialRevision, rid)
        if material:
            session.delete(material)


def delete_independent_material(session_factory, *, material_id: str, mode: str = "cascade", scope_id: str = "local", expected_impact_token: str | None = None) -> None:
    """Delete independent source and optionally cascade its frozen batch graph."""
    from app.privacy import JournalUnavailable, _journal, _records, installation_id
    if scope_id != "local":
        raise DeletionPlanError("unsupported private scope")
    installation_id()
    with _journal() as stream:
        records = _records(stream)
        found = [record for record in records if record.get("resource_type") == "material" and record["resource_id"] == material_id]
        retired = frozenset(identifier for record in records if record.get("resource_type") == "material" for identifier in record["revision_ids"])
        with session_factory() as session:
            intent = MaterialDeletionIntent.model_validate(found[0]) if found else freeze_material_intent(
                session, material_id=material_id, mode=mode, scope_id=scope_id, retired_revision_ids=retired)
            if intent.scope_id != scope_id or intent.mode != mode:
                raise JournalUnavailable("Material deletion intent conflict")
            if expected_impact_token is not None and expected_impact_token != _impact_token(intent):
                raise DeletionImpactChanged("material deletion impact changed")
            if not found:
                stream.seek(0, os.SEEK_END)
                stream.write(json.dumps(intent.model_dump(mode="json"), separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            apply_material_intent(session, intent)
            session.commit()


def require_material_guard(session: Session, *, material_id: str, revision_id: str | None = None,
                           scope_id: str = "local") -> None:
    """Check durable retirement before any future independent-material read."""
    require_v2_guard(session, scope_id=scope_id)
    guard = session.info["v2_journal_guard"]
    if material_id in guard["deleted_material_ids"] or (revision_id is not None and revision_id in guard["deleted_revision_ids"]):
        raise V2Deleted("material was deleted")
    query = select(MaterialRevision).where(MaterialRevision.material_id == material_id)
    if revision_id is not None:
        query = query.where(MaterialRevision.id == revision_id)
    rows = session.scalars(query).all()
    if not rows or any(row.scope_id != scope_id or not (
        (row.retention == "independent" and row.owner_batch_id is None) or
        (row.retention == "batch" and row.owner_batch_id == guard["batch_id"] and guard["batch_id"] is not None)
    ) for row in rows):
        raise V2Deleted("material unavailable")


def _impact_token(intent: MaterialDeletionIntent) -> str:
    import hashlib
    payload = json.dumps(intent.model_dump(mode="json"), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def preview_independent_material_deletion(session_factory, *, material_id: str, mode: str = "cascade",
                                          scope_id: str = "local") -> dict:
    """Preview affected batches; deletion recomputes under lock at intent time."""
    with v2_write_transaction(session_factory, scope_id=scope_id) as session:
        require_material_guard(session, scope_id=scope_id, material_id=material_id)
        retired = frozenset(session.info["v2_journal_guard"]["deleted_revision_ids"])
        intent = freeze_material_intent(session, material_id=material_id, mode=mode,
                                        scope_id=scope_id, retired_revision_ids=retired)
        return {"material_id": material_id, "mode": intent.mode,
                "revision_count": len(intent.revision_ids),
                "affected_batch_ids": [item.resource_id for item in intent.affected_batches],
                "impact_token": _impact_token(intent)}
