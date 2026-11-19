from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from app.db import database_url, make_engine
from app.runtime import ConfigurationError, WorkerHeartbeat, database_health, validate_configuration, worker_health


ROOT = Path(__file__).resolve().parents[1]


def test_database_credentials_are_url_encoded_without_loss(monkeypatch):
    monkeypatch.delenv("COACH_DATABASE_URL", raising=False)
    monkeypatch.setenv("COACH_DB_HOST", "postgres")
    monkeypatch.setenv("COACH_DB_PASSWORD", "fictional $ @ / # % password")
    monkeypatch.setenv("COACH_DB_USER", "fixture")
    monkeypatch.setenv("COACH_DB_NAME", "fixture")
    url = make_url(database_url())
    assert url.drivername == "postgresql+psycopg"
    assert url.password == "fictional $ @ / # % password"
    assert url.host == "postgres"
    monkeypatch.setenv("COACH_DATABASE_URL", "sqlite:///:memory:")
    assert database_url() == "sqlite:///:memory:"


def test_sqlite_foreign_keys_are_enabled_and_health_requires_migration():
    engine = make_engine("sqlite:///:memory:")
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("PRAGMA foreign_keys")) == 1
        assert database_health(engine)["error"] == "DATABASE_UNAVAILABLE"
        head = ScriptDirectory(str(ROOT / "alembic")).get_current_head()
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            connection.execute(text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": head})
        assert database_health(engine)["ok"] is True
        with engine.begin() as connection:
            connection.execute(text("UPDATE alembic_version SET version_num='not-current'"))
        assert database_health(engine)["error"] == "MIGRATION_REQUIRED"
    finally:
        engine.dispose()


def test_worker_heartbeat_detects_missing_expired_stalled_and_stopped(tmp_path, monkeypatch):
    monkeypatch.setenv("COACH_DATA_DIR", str(tmp_path))
    assert worker_health()["ok"] is False
    heartbeat = WorkerHeartbeat(interval=0.01)
    heartbeat.start()
    try:
        assert worker_health()["ok"] is True
        assert (tmp_path / ".worker-heartbeat.json").stat().st_mode & 0o777 == 0o600
    finally:
        heartbeat.close()
    assert worker_health()["error"] == "WORKER_STOPPED"
    now = time.time()
    path = tmp_path / ".worker-heartbeat.json"
    path.write_text(json.dumps({"state": "running", "updated_at": now-60, "progress_at": now}))
    assert worker_health()["error"] == "WORKER_HEARTBEAT_EXPIRED"
    path.write_text(json.dumps({"state": "running", "updated_at": now, "progress_at": now-1000}))
    assert worker_health()["error"] == "WORKER_STALLED"
    path.write_text(json.dumps({"state": "running", "updated_at": now+60, "progress_at": now}))
    assert worker_health()["ok"] is False
    path.write_text("[]")
    assert worker_health()["ok"] is False


def test_worker_drains_active_batch_without_claiming_guide(tmp_path, monkeypatch):
    from app import worker
    from app.providers import LocalProvider
    monkeypatch.setenv("COACH_DATA_DIR", str(tmp_path))
    stop = threading.Event()
    calls = []
    def finish_batch(_sessions, _provider, *, progress):
        progress()
        calls.append("batch")
        stop.set()  # The signal handler uses the same event during an active call.
        return True
    def claim_guide(_sessions, _provider):
        calls.append("guide")
        return True
    monkeypatch.setattr(worker, "process_batch_once", finish_batch)
    monkeypatch.setattr(worker, "process_guide_once", claim_guide)
    worker.run_worker(LocalProvider(), None, stop)
    assert calls == ["batch"]
    assert worker_health()["error"] == "WORKER_STOPPED"


@pytest.mark.parametrize("provider", ["unknown", "", "compatible"])
def test_configuration_failures_do_not_start_an_offline_fallback(provider, monkeypatch):
    monkeypatch.setenv("COACH_PROVIDER", provider)
    monkeypatch.setenv("COACH_API_BASE_URL", "")
    monkeypatch.setenv("COACH_MODEL", "")
    with pytest.raises(ConfigurationError):
        validate_configuration()


def test_container_configuration_errors_do_not_print_credentials(tmp_path):
    environment = dict(os.environ)
    environment.update({"COACH_PROVIDER": "compatible", "COACH_API_BASE_URL": "", "COACH_MODEL": "", "COACH_API_KEY": "fictional-sensitive-value", "COACH_DATABASE_URL": "sqlite:///:memory:", "COACH_DATA_DIR": str(tmp_path), "PYTHONDONTWRITEBYTECODE": "1"})
    result = subprocess.run([sys.executable, "scripts/container_entrypoint.py", "worker"], cwd=ROOT, env=environment, capture_output=True, text=True)
    assert result.returncode == 1
    assert "fictional-sensitive-value" not in result.stdout + result.stderr
    assert "配置错误" in result.stderr


@pytest.mark.parametrize("endpoint,key", [("https://", "fixture"), ("http://localhost.example.com", "fixture"), ("https://model.example.com", ""), ("https://user:password@model.example.com", "fixture")])
def test_compatible_configuration_rejects_bad_remote_endpoints(endpoint, key, monkeypatch):
    monkeypatch.setenv("COACH_PROVIDER", "compatible")
    monkeypatch.setenv("COACH_API_BASE_URL", endpoint)
    monkeypatch.setenv("COACH_MODEL", "fictional-model")
    monkeypatch.setenv("COACH_API_KEY", key)
    with pytest.raises(ConfigurationError):
        validate_configuration()
