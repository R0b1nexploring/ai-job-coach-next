"""Verify empty/historical SQLite upgrades and both deployed 0004 schemas."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def migrate(database: Path, *arguments: str) -> None:
    env = {**os.environ, "COACH_DATABASE_URL": f"sqlite:///{database}", "COACH_PROVIDER": "local", "PYTHONDONTWRITEBYTECODE": "1"}
    subprocess.run([sys.executable, "-m", "alembic", *arguments], cwd=ROOT, env=env, check=True)


def check_upgrade(database: Path) -> None:
    migrate(database, "upgrade", "0001")
    stamp = "2026-10-01 00:00:00"
    snapshot = {}
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO batches (id,idempotency_key,input_hash,jd_text,material_text,material_confirmed,status,error_code,lease_until,attempts,created_at,updated_at,plans) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("fictional-batch", "fictional-upgrade", "0" * 64, "Fictional engineering role.", "Fictional coursework.", False, "ready", None, None, 1, stamp, stamp, json.dumps({"projects": []})),
        )
        for kind in ("job", "blueprint"):
            resume_id = f"fictional-{kind}"
            content_hash = ("1" if kind == "job" else "2") * 64
            items = json.dumps([{"id": f"item-{kind}", "text": "Fictional upgrade fixture.", "verification_status": "ai_generated", "achieved": False}])
            connection.execute(
                "INSERT INTO resumes (id,batch_id,kind,title,summary,items,content_hash,created_at) VALUES (?,?,?,?,?,?,?,?)",
                (resume_id, "fictional-batch", kind, "Fictional resume", "Upgrade fixture.", items, content_hash, stamp),
            )
            connection.execute(
                "INSERT INTO guides (id,batch_id,resume_id,kind,status,content_hash,sections,error_code,attempts,lease_until) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (f"guide-{kind}", "fictional-batch", resume_id, kind, "ready", content_hash, json.dumps({"entries": []}), None, 1, None),
            )
        # The table names and column names below come only from these test databases.
        for table in ("batches", "resumes", "guides"):
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
            snapshot[table] = (columns, connection.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY id").fetchall())
    migrate(database, "upgrade", "head")
    migrate(database, "check")
    with sqlite3.connect(database) as connection:
        for table, (columns, expected) in snapshot.items():
            observed = connection.execute(f"SELECT {','.join(columns)} FROM {table} ORDER BY id").fetchall()
            if observed != expected:
                raise RuntimeError(f"{table}: historical fictional values changed")
        if connection.execute("SELECT count(*) FROM resumes WHERE revision=1").fetchone() != (2,):
            raise RuntimeError("resume revision initialization failed")
        if connection.execute("SELECT count(*) FROM resume_revisions WHERE revision=1").fetchone() != (2,):
            raise RuntimeError("resume revision history backfill failed")
        mismatches = connection.execute(
            "SELECT count(*) FROM resume_revisions h JOIN resumes r ON h.resume_id=r.id WHERE h.title!=r.title OR h.summary!=r.summary OR h.items!=r.items OR h.content_hash!=r.content_hash OR h.created_at!=r.created_at"
        ).fetchone()[0]
        if mismatches:
            raise RuntimeError("resume revision history differs from its original source")
    print("[passed] previous SQLite schema data and revision history preserved", flush=True)


def _insert(connection: sqlite3.Connection, table: str, values: dict) -> None:
    # All table/column identifiers are controlled fixture definitions below.
    columns = list(values)
    encoded = [json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value for value in values.values()]
    connection.execute(f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", encoded)


def check_0004_compatibility(database: Path, prior_schema: str) -> None:
    migrate(database, "upgrade", "0003")
    stamp = "2026-10-03 00:00:00"
    with sqlite3.connect(database) as connection:
        _insert(connection, "batches", {
            "id": "fictional-batch", "idempotency_key": "fictional-compatibility",
            "input_hash": "0" * 64, "jd_text": "Fictional engineering role.",
            "material_text": "Fictional coursework.", "material_confirmed": False,
            "status": "ready", "attempts": 1, "created_at": stamp, "updated_at": stamp,
            "plans": {"projects": []},
        })
        for kind in ("job", "blueprint"):
            resume_id = "fictional-" + kind
            snapshots = [{
                "title": "Fictional resume", "summary": f"Fictional revision {revision}.",
                "items": [{"id": "fictional-item", "text": f"Fictional item {revision}.", "verification_status": "ai_generated", "achieved": False}],
                "content_hash": str(revision) * 64, "semantic_hash": ("a" if revision == 1 else "b") * 64,
                "plans": [], "created_at": stamp,
            } for revision in (1, 2)]
            current = snapshots[-1]
            _insert(connection, "resumes", {"id": resume_id, "batch_id": "fictional-batch", "kind": kind, "revision": 2, **current})
            for revision, value in enumerate(snapshots, start=1):
                _insert(connection, "resume_revisions", {"id": f"history-{kind}-{revision}", "resume_id": resume_id, "revision": revision, **value})
            frozen = {key: current[key] for key in ("title", "summary", "items")}
            guide_id = "guide-" + kind
            _insert(connection, "guides", {
                "id": guide_id, "batch_id": "fictional-batch", "resume_id": resume_id,
                "kind": kind, "status": "ready", "content_hash": current["content_hash"],
                "semantic_hash": current["semantic_hash"], "revision": 1, "resume_revision": 2,
                "sections": {"entries": [{"item_id": "fictional-item", "answer": "Fictional guide."}]}, "attempts": 1,
            })
            _insert(connection, "guide_revisions", {
                "id": "guide-history-" + kind, "guide_id": guide_id,
                "batch_id": "fictional-batch", "revision": 1, "resume_revision": 2,
                "content_hash": current["content_hash"], "semantic_hash": current["semantic_hash"],
                "resume_snapshot": frozen, "plans": [], "jd_text": "Fictional engineering role.",
                "status": "ready", "sections": {"entries": []}, "created_at": stamp, "completed_at": stamp,
            })
            _insert(connection, "task_executions", {
                "id": "execution-" + kind, "batch_id": "fictional-batch", "guide_id": guide_id,
                "task_type": "guide", "claim_token": "fictional-token-" + kind, "attempt": 1,
                "input_hash": current["semantic_hash"], "input_snapshot": {"resume": frozen, "plans": []},
                "status": "ready", "started_at": stamp, "finished_at": stamp,
            })
    profile = {"name": "Fictional Person", "email": "fixture@example.com", "education": "Fictional University"}
    if prior_schema in {"0004_header", "0004_both"}:
        migrate(database, "upgrade", "0004")
    if prior_schema in {"0004_profile", "0004_both"}:
        with sqlite3.connect(database) as connection:
            connection.execute("ALTER TABLE batches ADD COLUMN profile JSON")
            connection.execute("UPDATE batches SET profile=?", (json.dumps(profile),))
    if prior_schema == "0004_profile":
        migrate(database, "stamp", "0004")
    if prior_schema in {"0004_header", "0004_both"}:
        with sqlite3.connect(database) as connection:
            for name in ("resumes", "resume_revisions"):
                rows = connection.execute(f"SELECT id FROM {name}").fetchall()
                for (row_id,) in rows:
                    connection.execute(f"UPDATE {name} SET header=? WHERE id=?", (json.dumps({"name": "Fictional " + row_id, "city": "Fictional City"}), row_id))
    original = {}
    with sqlite3.connect(database) as connection:
        for name in ("batches", "resumes", "resume_revisions", "guides", "guide_revisions", "task_executions"):
            columns = [row[1] for row in connection.execute(f"PRAGMA table_info({name})")]
            original[name] = (columns, connection.execute(f"SELECT {','.join(columns)} FROM {name} ORDER BY id").fetchall())
    migrate(database, "upgrade", "head")
    migrate(database, "check")
    with sqlite3.connect(database) as connection:
        if connection.execute("SELECT version_num FROM alembic_version").fetchone() != ("0007",):
            raise RuntimeError(f"{prior_schema}: expected current revision 0007")
        for name, (columns, rows) in original.items():
            if connection.execute(f"SELECT {','.join(columns)} FROM {name} ORDER BY id").fetchall() != rows:
                raise RuntimeError(f"{prior_schema}: original {name} values changed")
        for name in ("resumes", "resume_revisions"):
            info = {row[1]: row for row in connection.execute(f"PRAGMA table_info({name})")}
            if info["header"][3:5] != (1, "'{}'"):
                raise RuntimeError(f"{prior_schema}: {name}.header missing non-null/default contract")
            for row_id, encoded in connection.execute(f"SELECT id,header FROM {name}"):
                expected = profile if prior_schema == "0004_profile" else ({"name": "Fictional " + row_id, "city": "Fictional City"} if prior_schema in {"0004_header", "0004_both"} else {})
                if json.loads(encoded) != expected:
                    raise RuntimeError(f"{prior_schema}: {name} header preservation/capture failed")
    print(f"[passed] {prior_schema} -> 0007: original values, hashes, revisions and frozen inputs preserved", flush=True)


def main() -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="ai-job-coach-next-migrations-") as temporary:
            directory = Path(temporary)
            migrate(directory / "empty.sqlite", "upgrade", "head")
            migrate(directory / "empty.sqlite", "check")
            check_upgrade(directory / "historical.sqlite")
            for prior_schema in ("0003", "0004_profile", "0004_header", "0004_both"):
                check_0004_compatibility(directory / f"{prior_schema}.sqlite", prior_schema)
    except (subprocess.CalledProcessError, sqlite3.Error, OSError, RuntimeError) as error:
        print(f"[failed] isolated SQLite migration check: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
