"""Verify empty and historical SQLite upgrades using disposable fictional data."""
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


def main() -> int:
    try:
        with tempfile.TemporaryDirectory(prefix="ai-job-coach-next-migrations-") as temporary:
            directory = Path(temporary)
            migrate(directory / "empty.sqlite", "upgrade", "head")
            migrate(directory / "empty.sqlite", "check")
            check_upgrade(directory / "historical.sqlite")
    except (subprocess.CalledProcessError, sqlite3.Error, OSError, RuntimeError) as error:
        print(f"[failed] isolated SQLite migration check: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
