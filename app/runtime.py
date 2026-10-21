"""Small, sanitized health checks for the personal API and worker install."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import threading
import time
from uuid import uuid4
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]


class ConfigurationError(ValueError):
    """Only fixed, safe messages belong in this exception."""


def data_directory() -> Path:
    return Path(os.getenv("COACH_DATA_DIR", "./data"))


def heartbeat_path() -> Path:
    return data_directory() / ".worker-heartbeat.json"


def _positive_setting(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError:
        raise ConfigurationError(f"{name} 必须是正数") from None
    if not math.isfinite(value) or value <= 0:
        raise ConfigurationError(f"{name} 必须是正数")
    return value


def validate_configuration() -> dict[str, str]:
    provider = os.getenv("COACH_PROVIDER", "local").strip()
    if provider not in {"local", "compatible"}:
        raise ConfigurationError("COACH_PROVIDER 必须为 local 或 compatible")
    if provider == "compatible":
        from app.providers import CompatibleProvider
        try:
            CompatibleProvider()
            endpoint = urlsplit(os.getenv("COACH_API_BASE_URL", ""))
            if not endpoint.hostname or endpoint.username or endpoint.password:
                raise ValueError()
            if endpoint.scheme != "https" and not (endpoint.scheme == "http" and endpoint.hostname in {"localhost", "127.0.0.1"}):
                raise ValueError()
            if endpoint.scheme == "https" and not os.getenv("COACH_API_KEY", "").strip():
                raise ValueError()
        except (ValueError, TypeError):
            raise ConfigurationError("兼容模型配置无效，请检查地址、模型、密钥和超时") from None
    _positive_setting("COACH_TIMEOUT_SECONDS", 90)
    interval = _positive_setting("COACH_WORKER_HEARTBEAT_SECONDS", 5)
    max_age = _positive_setting("COACH_WORKER_HEARTBEAT_MAX_AGE", 30)
    _positive_setting("COACH_WORKER_STALL_SECONDS", 600)
    if interval >= max_age:
        raise ConfigurationError("worker 心跳间隔必须短于健康检查过期时间")
    from sqlalchemy.engine import make_url
    try:
        from app.db import database_url
        url = make_url(database_url())
        if url.get_backend_name() not in {"sqlite", "postgresql", "postgres"}:
            raise ValueError()
        if url.get_backend_name() != "sqlite" and not all((url.host, url.database, url.username, url.password)):
            raise ValueError()
    except (ValueError, TypeError):
        raise ConfigurationError("数据库配置无效，请检查连接参数") from None
    return {"provider": provider, "database": url.get_backend_name()}


def database_health(engine_override=None) -> dict:
    from alembic.script import ScriptDirectory
    from sqlalchemy import text
    from app.db import engine
    selected = engine_override if engine_override is not None else engine
    backend = selected.dialect.name
    try:
        expected = set(ScriptDirectory(str(ROOT / "alembic")).get_heads())
        with selected.connect() as connection:
            connection.execute(text("SELECT 1"))
            current = set(connection.scalars(text("SELECT version_num FROM alembic_version")))
        if current != expected:
            return {"ok": False, "engine": backend, "error": "MIGRATION_REQUIRED"}
        return {"ok": True, "engine": backend, "revisions": sorted(current)}
    except Exception:
        # Never stringify database exceptions: they can carry URLs or SQL values.
        return {"ok": False, "engine": backend, "error": "DATABASE_UNAVAILABLE"}


def worker_health(max_age_seconds: float | None = None) -> dict:
    try:
        path = heartbeat_path()
        if path.stat().st_size > 4096:
            raise ValueError()
        record = json.loads(path.read_text(encoding="utf-8"))
        timestamp = float(record["updated_at"])
        progress = float(record["progress_at"])
        if not all(math.isfinite(value) for value in (timestamp, progress)):
            raise ValueError()
        now = time.time()
        age = now - timestamp
        max_age = max_age_seconds if max_age_seconds is not None else _positive_setting("COACH_WORKER_HEARTBEAT_MAX_AGE", 30)
        if age < -5 or progress > now + 5:
            raise ValueError()
        if record.get("state") != "running":
            return {"ok": False, "error": "WORKER_STOPPED"}
        if age > max_age:
            return {"ok": False, "error": "WORKER_HEARTBEAT_EXPIRED"}
        if now - progress > _positive_setting("COACH_WORKER_STALL_SECONDS", 600):
            return {"ok": False, "error": "WORKER_STALLED"}
        return {"ok": True, "age_seconds": round(max(0, age), 2)}
    except (OSError, ValueError, TypeError, KeyError):
        return {"ok": False, "error": "WORKER_HEARTBEAT_UNAVAILABLE"}


@dataclass
class WorkerHeartbeat:
    """A thread stays alive during bounded model calls, without touching task leases."""

    interval: float = 5

    def __post_init__(self):
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._identity = uuid4().hex
        self._progress = time.time()
        self._state = "running"
        self._failure = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="worker-heartbeat")

    def _write(self):
        path = heartbeat_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            record = {"version": 1, "instance": self._identity, "updated_at": time.time(), "progress_at": self._progress, "state": self._state}
        temporary = path.with_name(f"{path.name}.{self._identity}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(record, output)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _run(self):
        while not self._stop.wait(self.interval):
            try:
                self._write()
            except OSError:
                self._failure = True
                return

    def start(self):
        self._write()  # Permission errors must fail startup, not hide in a thread.
        self._thread.start()

    def progressed(self):
        if self._failure:
            raise RuntimeError("worker 心跳写入失败")
        with self._lock:
            self._progress = time.time()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=self.interval + 1)
        with self._lock:
            self._state = "stopped"
        self._write()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("check", choices=("check-worker", "check-db"))
    arguments = parser.parse_args()
    result = worker_health() if arguments.check == "check-worker" else database_health()
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
