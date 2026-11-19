"""Copy the independent deletion journal without permitting its rollback."""
from __future__ import annotations

import argparse
import hashlib
from io import BytesIO, StringIO
import json
import os
from pathlib import Path
import tarfile

from app.db import SessionLocal
from app.models import Batch, FactSnapshot, MaterialRevision
from app.privacy import _ids, _intent_keys, _journal, _durable_file, installation_id, journal_path, JournalUnavailable
from sqlalchemy import select


def export_journal(target: Path) -> None:
    with _journal() as stream:
        _ids(stream)
        stream.seek(0)
        contents = {"deletions.jsonl": stream.read().encode(), "installation.json": (journal_path().parent / "installation.json").read_bytes()}
        installation_id()
        manifest = {name: hashlib.sha256(value).hexdigest() for name, value in contents.items()}
        contents["manifest.json"] = json.dumps(manifest).encode()
        with tarfile.open(target, "x:gz") as archive:
            for name, value in contents.items():
                member = tarfile.TarInfo(name)
                member.size = len(value)
                member.mode = 0o600
                archive.addfile(member, BytesIO(value))
        target.chmod(0o600)


def import_journal(source: Path, session_factory=SessionLocal) -> None:
    with tarfile.open(source, "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if set(names) != {"deletions.jsonl", "installation.json", "manifest.json"} or len(names) != 3 or any(not member.isfile() or member.size > 100 * 1024 * 1024 for member in members):
            raise JournalUnavailable("Journal archive invalid")
        values = {member.name: archive.extractfile(member).read() for member in members}
    manifest = json.loads(values.pop("manifest.json"))
    if set(manifest) != set(values) or any(manifest[name] != hashlib.sha256(value).hexdigest() for name, value in values.items()):
        raise JournalUnavailable("Journal checksum mismatch")
    incoming = json.loads(values["installation.json"])
    identifier = incoming["installation_id"]
    import re
    if incoming.get("format") != 1 or not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{32}", identifier):
        raise JournalUnavailable("Journal identity invalid")
    incoming_ids = _ids(StringIO(values["deletions.jsonl"].decode()))
    incoming_intents = _intent_keys(StringIO(values["deletions.jsonl"].decode()))
    if journal_path().exists():
        with _journal() as stream:
            current_ids = _ids(stream)
            current_intents = _intent_keys(stream)
            same_installation = installation_id() == identifier
            with session_factory() as session:
                has_business = any(session.scalar(select(model.id).limit(1)) is not None for model in (Batch, FactSnapshot, MaterialRevision))
            if (same_installation and not current_intents <= incoming_intents) or (not same_installation and (has_business or current_intents)):
                raise JournalUnavailable("Cannot roll back or replace existing deletion state")
    else:
        marker = Path(os.getenv("COACH_DATA_DIR", "./data")) / ".installation-id"
        identity = journal_path().parent / "installation.json"
        with session_factory() as session:
            has_business = any(session.scalar(select(model.id).limit(1)) is not None for model in (Batch, FactSnapshot, MaterialRevision))
        # An identity cannot prove which deletions a lost journal contained.
        # Enrollment is only safe for a genuinely new, empty installation.
        if marker.exists() or identity.exists() or has_business:
            raise JournalUnavailable("Missing existing deletion state cannot be replaced")
    journal_path().parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    data = Path(os.getenv("COACH_DATA_DIR", "./data"))
    data.mkdir(parents=True, exist_ok=True)
    # The lifecycle script has stopped API/worker. Each replacement is durable;
    # a crash during enrollment leaves startup closed until import is retried.
    for path, content in [(journal_path().parent / "installation.json", values["installation.json"].decode()), (journal_path(), values["deletions.jsonl"].decode()), (data / ".installation-id", identifier + "\n")]:
        temporary = path.with_name(path.name + ".import")
        temporary.unlink(missing_ok=True)
        _durable_file(temporary, content)
        temporary.replace(path)
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if os.geteuid() == 0:
            os.chown(path, 10001, 10001)
    if os.geteuid() == 0:
        os.chown(journal_path().parent, 10001, 10001)
        os.chown(data, 10001, 10001)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("export", "import"))
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        export_journal(args.path) if args.action == "export" else import_journal(args.path)
        print("独立删除记录已导出" if args.action == "export" else "独立删除记录已校验并恢复；请继续恢复业务备份")
        return 0
    except Exception:
        print("独立删除记录校验失败，拒绝回滚或替换；服务保持关闭")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
