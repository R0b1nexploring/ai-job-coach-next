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

from app.models import Batch


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
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise JournalUnavailable("Deletion journal unavailable") from error


def _ids(stream) -> set[str]:
    stream.seek(0)
    identifiers = set()
    for line in stream:
        record = json.loads(line)
        if not isinstance(record, dict):
            raise JournalUnavailable("Deletion journal invalid")
        identifier = record.get("batch_id", "")
        if record.get("version") != 1 or not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise JournalUnavailable("Deletion journal invalid")
        identifiers.add(identifier)
    return identifiers


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
                if session.scalar(select(Batch.id).limit(1)):
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
    identifiers = deleted_ids()  # Missing/corrupt journal fails closed on restore.
    with session_factory() as session:
        rows = session.scalars(select(Batch).where(Batch.id.in_(identifiers)).with_for_update()).all() if identifiers else []
        for row in rows:
            session.delete(row)
        session.commit()
        return len(rows)


def delete_batch(session_factory, identifier: str) -> None:
    installation_id()
    # The independent intent precedes all business mutations. A failed commit
    # remains hidden and is retried at startup or during restoration.
    with _journal() as stream:
        identifiers = _ids(stream)
        if identifier not in identifiers:
            with session_factory() as session:
                if session.get(Batch, identifier) is None:
                    from app.service import NotFound
                    raise NotFound()
            if not re.fullmatch(r"[a-f0-9]{32}", identifier):
                raise ValueError("invalid batch id")
            stream.seek(0, os.SEEK_END)
            stream.write(json.dumps({"version": 1, "batch_id": identifier, "deleted_at": datetime.now(timezone.utc).isoformat()}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        with session_factory() as session:
            row = session.scalar(select(Batch).where(Batch.id == identifier).with_for_update())
            if row:
                session.delete(row)
            session.commit()
