"""Personal-install deletion intents, kept outside restorable business backups."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from sqlalchemy import select

from app.models import Batch, FactSnapshot, MaterialRevision


class JournalUnavailable(Exception):
    pass


def journal_path() -> Path:
    return Path(os.getenv("COACH_JOURNAL_DIR", "./data/deletion-journal")) / "deletions.jsonl"


@contextmanager
def _journal(*, create: bool = False):
    path = journal_path()
    try:
        if create:
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_RDWR | (os.O_CREAT if create else 0), 0o600)
        with os.fdopen(descriptor, "r+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            yield stream
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise JournalUnavailable("Deletion journal unavailable") from error


def _records(stream) -> list[dict]:
    """Read every intent or fail closed; v1 remains byte-for-byte compatible."""
    from app.privacy_v2 import BatchDeletionIntent, MaterialDeletionIntent

    stream.seek(0)
    records = []
    seen: dict[tuple[str, str], dict] = {}
    for line in stream:
        record = json.loads(line)
        if not isinstance(record, dict):
            raise JournalUnavailable("Deletion journal invalid")
        if record.get("version") == 1:
            identifier = record.get("batch_id", "")
            if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
                raise JournalUnavailable("Deletion journal invalid")
        elif record.get("version") == 2:
            try:
                model = BatchDeletionIntent if record.get("resource_type") == "batch" else MaterialDeletionIntent
                record = model.model_validate(record).model_dump(mode="json")
            except ValueError:
                raise JournalUnavailable("Deletion journal invalid") from None
            identifier = record["resource_id"]
        else:
            raise JournalUnavailable("Deletion journal invalid")
        key = (record.get("resource_type", "batch"), identifier)
        if key in seen and seen[key] != record:
            raise JournalUnavailable("Deletion journal contains conflicting intents")
        seen[key] = record
        records.append(record)
    return records


def _ids(stream) -> set[str]:
    identifiers = set()
    for record in _records(stream):
        if record["version"] == 1:
            identifiers.add(record["batch_id"])
        elif record["resource_type"] == "batch":
            identifiers.add(record["resource_id"])
        elif record["mode"] == "cascade":
            identifiers.update(batch["resource_id"] for batch in record["affected_batches"])
    return identifiers


def _intent_keys(stream) -> set[str]:
    # Include the frozen graph and scope: comparing only batch IDs would permit
    # an older archive to silently discard a newer deletion intent.
    return {json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":")) for r in _records(stream)}


def _durable_file(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(value)
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def installation_id() -> str:
    try:
        record = json.loads((journal_path().parent / "installation.json").read_text())
        identifier = record["installation_id"]
        if record.get("format") != 1 or not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError()
        return identifier
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise JournalUnavailable("Installation identity unavailable") from error


def initialize_journal(session_factory=None, *, bootstrap: bool = False) -> None:
    data = Path(os.getenv("COACH_DATA_DIR", "./data"))
    marker = data / ".installation-id"
    identity = journal_path().parent / "installation.json"
    missing = not journal_path().exists()
    if missing:
        # Only a demonstrably empty first install can create deletion state.
        # Any existing identity/marker makes loss of the journal a hard failure.
        if not bootstrap or marker.exists() or identity.exists():
            raise JournalUnavailable("Deletion journal missing")
        if session_factory:
            with session_factory() as session:
                if any(session.scalar(select(model.id).limit(1)) for model in (Batch, FactSnapshot, MaterialRevision)):
                    raise JournalUnavailable("Existing business data requires its journal")
    with _journal(create=missing) as stream:
        _ids(stream)
        stream.flush()
        os.fsync(stream.fileno())
    # Make a newly created journal durable before a deletion can be accepted.
    descriptor = os.open(journal_path().parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    # Enrollment of the previous local prototype preserves all existing intents.
    # An identity binds backups to this journal even if every business volume is lost.
    if not identity.exists():
        identifier = marker.read_text().strip() if marker.exists() else uuid4().hex
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise JournalUnavailable("Installation marker invalid")
        try:
            _durable_file(identity, json.dumps({"format": 1, "installation_id": identifier}) + "\n")
        except FileExistsError:
            pass
    identifier = installation_id()
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    if marker.exists():
        if marker.read_text().strip() != identifier:
            raise JournalUnavailable("Installation identity mismatch")
    else:
        try:
            _durable_file(marker, identifier + "\n")
        except FileExistsError:
            if marker.read_text().strip() != identifier:
                raise JournalUnavailable("Installation identity mismatch")


def deleted_ids() -> set[str]:
    installation_id()
    with _journal() as stream:
        return _ids(stream)


def replay_deletions(session_factory) -> int:
    from app.models import FactSnapshot
    from app.privacy_v2 import BatchDeletionIntent, MaterialDeletionIntent, DeletionPlanError, validate_replay, apply_material_intent

    installation_id()
    # Recovery must also stop all API and worker processes; this lock prevents
    # cooperating local processes from observing or changing a partial replay.
    with _journal() as stream:
        records = _records(stream)
        with session_factory() as session:
            count = 0
            for record in records:
                if record["version"] == 1:
                    row = session.scalar(select(Batch).where(Batch.id == record["batch_id"]).with_for_update())
                    if row:
                        if row.schema_version == 2:
                            raise JournalUnavailable("Legacy intent cannot delete v2 data")
                        session.delete(row)
                        count += 1
                elif record["resource_type"] == "material":
                    intent = MaterialDeletionIntent.model_validate(record)
                    try:
                        count += sum(1 for item in intent.affected_batches if intent.mode == "cascade" and session.get(Batch, item.resource_id) is not None)
                        apply_material_intent(session, intent)
                    except DeletionPlanError:
                        raise JournalUnavailable("V2 material deletion replay invalid") from None
                else:
                    intent = BatchDeletionIntent.model_validate(record)
                    try:
                        snapshot_ids = validate_replay(session, intent)
                    except DeletionPlanError:
                        raise JournalUnavailable("V2 deletion replay invalid") from None
                    row = session.get(Batch, intent.resource_id)
                    if row:
                        session.delete(row)
                        count += 1
                    for snapshot_id in snapshot_ids:
                        snapshot = session.get(FactSnapshot, snapshot_id)
                        if snapshot:
                            session.delete(snapshot)
                    for material_id in intent.owned_material_revision_ids:
                        material = session.get(MaterialRevision, material_id)
                        if material:
                            session.delete(material)
            session.commit()
            return count


def delete_batch(session_factory, identifier: str) -> None:
    installation_id()
    # The independent intent precedes all business mutations. A failed commit
    # remains hidden and is retried at startup or during restoration.
    with _journal() as stream:
        identifiers = _ids(stream)
        if identifier not in identifiers:
            with session_factory() as session:
                batch = session.get(Batch, identifier)
                if batch is None:
                    from app.service import NotFound
                    raise NotFound()
                if batch.schema_version == 2:
                    raise JournalUnavailable("Use v2 deletion protocol")
            if not re.fullmatch(r"[a-f0-9]{32}", identifier):
                raise ValueError("invalid batch id")
            stream.seek(0, os.SEEK_END)
            stream.write(json.dumps({"version": 1, "batch_id": identifier, "deleted_at": datetime.now(timezone.utc).isoformat()}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        with session_factory() as session:
            row = session.scalar(select(Batch).where(Batch.id == identifier).with_for_update())
            if row:
                if row.schema_version == 2:
                    raise JournalUnavailable("Use v2 deletion protocol")
                session.delete(row)
            session.commit()
