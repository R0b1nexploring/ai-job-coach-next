"""Checksummed, closed-writer backup payloads for the personal Docker install."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import tarfile


class InvalidArchive(ValueError):
    pass


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def pack(directory: Path, data: Path, destination: Path, schema: str, installation: str) -> dict:
    data = data.resolve()
    database = directory / "database.dump"
    if not database.is_file() or database.stat().st_size == 0:
        raise InvalidArchive("Database dump unavailable")
    files = {"database.dump": database}
    journal = Path(os.getenv("COACH_JOURNAL_DIR", "./data/deletion-journal")).resolve()
    for path in sorted(data.rglob("*")):
        if path.is_relative_to(journal) or path.name.startswith(".worker-heartbeat"):
            continue
        if path.is_symlink():
            raise InvalidArchive("Data symlinks are not supported")
        if path.is_file():
            files["data/" + path.relative_to(data).as_posix()] = path
    manifest = {"format": 1, "installation_id": installation, "schema": schema, "created_at": datetime.now(timezone.utc).isoformat(), "files": {name: {"sha256": digest(path), "bytes": path.stat().st_size} for name, path in files.items()}}
    manifest_path = directory / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with tarfile.open(temporary, "w:gz") as archive:
        archive.add(manifest_path, arcname="manifest.json", recursive=False)
        for name, path in files.items():
            archive.add(path, arcname=name, recursive=False)
    temporary.chmod(0o600)
    temporary.replace(destination)
    return manifest


def verify(archive_path: Path, destination: Path | None = None, installation: str | None = None) -> dict:
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            if len(members) > 10000 or len(set(names)) != len(names) or sum(member.size for member in members) > 10 * 1024**3:
                raise InvalidArchive("Archive inventory invalid")
            for member in members:
                parts = PurePosixPath(member.name)
                if not member.isfile() or parts.is_absolute() or ".." in parts.parts or str(parts) != member.name or not (member.name in {"database.dump", "manifest.json"} or member.name.startswith("data/")):
                    raise InvalidArchive("Archive path invalid")
            metadata = archive.getmember("manifest.json")
            if metadata.size > 2 * 1024 * 1024:
                raise InvalidArchive("Archive manifest too large")
            manifest = json.load(archive.extractfile(metadata))
            inventory = manifest["files"]
            import re
            if manifest["format"] != 1 or not isinstance(manifest["schema"], str) or not isinstance(manifest.get("installation_id"), str) or not re.fullmatch(r"[a-f0-9]{32}", manifest["installation_id"]) or not isinstance(inventory, dict) or set(inventory) != set(names) - {"manifest.json"} or "database.dump" not in inventory:
                raise InvalidArchive("Archive manifest mismatch")
            if installation is not None and manifest["installation_id"] != installation:
                raise InvalidArchive("Backup requires its original independent deletion journal")
            for member in members:
                if member.name == "manifest.json":
                    continue
                expected = inventory[member.name]
                value = hashlib.sha256()
                with archive.extractfile(member) as source:
                    for block in iter(lambda: source.read(1024 * 1024), b""):
                        value.update(block)
                if expected["bytes"] != member.size or expected["sha256"] != value.hexdigest():
                    raise InvalidArchive("Archive checksum mismatch")
            # Extract only after every path, length and checksum has passed.
            if destination:
                destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                if any(destination.iterdir()):
                    raise InvalidArchive("Extraction directory must be empty")
                for member in members:
                    target = destination / member.name
                    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                    with archive.extractfile(member) as source, target.open("xb") as output:
                        shutil.copyfileobj(source, output)
                    target.chmod(0o600)
            return manifest
    except (tarfile.TarError, KeyError, TypeError, ValueError, OSError) as error:
        if isinstance(error, InvalidArchive):
            raise
        raise InvalidArchive("Archive unreadable or incomplete") from error


def restore_data(directory: Path, data: Path) -> int:
    from app.db import SessionLocal
    from app.models import Batch, Guide, TaskExecution, utcnow
    from app.privacy import deleted_ids, replay_deletions
    from sqlalchemy import update
    deleted_ids()  # Independent journal must survive, never manufacture one.
    journal = Path(os.getenv("COACH_JOURNAL_DIR", "./data/deletion-journal")).resolve()
    data = data.resolve()
    source = directory / "data"
    # Validate the whole source before touching the live directory. Even a
    # checksummed business archive must never replace the independent journal.
    if source.exists():
        for path in source.rglob("*"):
            target = (data / path.relative_to(source)).resolve()
            if target == journal or target.is_relative_to(journal):
                raise InvalidArchive("Business archive contains independent deletion state")
    data.mkdir(parents=True, exist_ok=True)
    def clear_directory(parent: Path):
        for path in parent.iterdir():
            resolved = path.resolve()
            if resolved == journal or resolved.is_relative_to(journal):
                continue
            if path.is_dir() and not path.is_symlink():
                if journal.is_relative_to(resolved):
                    clear_directory(path)
                else:
                    shutil.rmtree(path)
            else:
                path.unlink()
    clear_directory(data)
    if source.exists():
        for path in source.rglob("*"):
            target = data / path.relative_to(source)
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
            elif path.is_file():
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(path, target)
                target.chmod(0o600)
    for path in [data, *data.rglob("*")]:
        if os.geteuid() == 0:
            os.chown(path, 10001, 10001)
    deleted = replay_deletions(SessionLocal)
    now = utcnow()
    with SessionLocal() as session:
        for model in (Batch, Guide):
            session.execute(update(model).where(model.status == "running").values(claim_token=None, lease_until=now - timedelta(seconds=1)).execution_options(synchronize_session=False))
        session.execute(update(TaskExecution).where(TaskExecution.status == "running").values(status="lease_expired", error_code="RESTORED", finished_at=now))
        session.commit()
    return deleted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("pack", "verify", "restore-data"))
    parser.add_argument("path", type=Path)
    arguments = parser.parse_args()
    try:
        if os.getenv("COACH_DATABASE_URL", "").strip():
            raise InvalidArchive("Docker backup requires its managed PostgreSQL database")
        from app.privacy import deleted_ids, installation_id
        deleted_ids()
        if arguments.action == "pack":
            from alembic.script import ScriptDirectory
            from app.runtime import ROOT, data_directory, database_health
            state = database_health()
            if not state["ok"] or state["engine"] != "postgresql":
                raise InvalidArchive("Database schema unavailable")
            manifest = pack(arguments.path, data_directory(), arguments.path / "backup.tar.gz", ScriptDirectory(str(ROOT / "alembic")).get_current_head(), installation_id())
            print(json.dumps({"status": "packed", "files": len(manifest["files"]), "schema": manifest["schema"]}))
        elif arguments.action == "verify":
            manifest = verify(arguments.path, arguments.path.parent / "verified", installation_id())
            print(json.dumps({"status": "verified", "files": len(manifest["files"]), "schema": manifest["schema"]}))
        else:
            from app.runtime import data_directory
            deleted = restore_data(arguments.path, data_directory())
            print(json.dumps({"status": "restored", "deletions_replayed": deleted}))
    except Exception:
        # Exceptions may include connection strings and private filenames.
        print("备份或恢复校验失败；服务保持关闭，请检查归档、schema 和独立删除记录", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
