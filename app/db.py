from __future__ import annotations

import os
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


class DatabaseConfigurationError(ValueError):
    pass


def database_url() -> str:
    configured = os.getenv("COACH_DATABASE_URL", "").strip()
    if configured:
        return configured
    if os.getenv("COACH_DB_HOST", "").strip():
        # Construct, never interpolate, credentials: $, spaces, @ and % are data.
        try:
            port = int(os.getenv("COACH_DB_PORT", "5432"))
            if not 1 <= port <= 65535:
                raise ValueError()
        except ValueError:
            raise DatabaseConfigurationError("数据库端口配置无效") from None
        return URL.create(
            "postgresql+psycopg",
            username=os.getenv("COACH_DB_USER", "coach"),
            password=os.getenv("COACH_DB_PASSWORD", ""),
            host=os.environ["COACH_DB_HOST"],
            port=port,
            database=os.getenv("COACH_DB_NAME", "coach"),
        ).render_as_string(hide_password=False)
    directory = Path(os.getenv("COACH_DATA_DIR", "./data"))
    return URL.create("sqlite", database=str(directory / "prototype.sqlite")).render_as_string(hide_password=False)


def make_engine(url: str | None = None):
    try:
        resolved = make_url(url or database_url())
    except (ArgumentError, ValueError):
        raise DatabaseConfigurationError("数据库连接配置无效") from None
    if resolved.drivername in {"postgres", "postgresql"}:
        resolved = resolved.set(drivername="postgresql+psycopg")
    sqlite = resolved.get_backend_name() == "sqlite"
    if sqlite and resolved.database and resolved.database != ":memory:":
        Path(resolved.database).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        resolved,
        connect_args={"check_same_thread": False} if sqlite else {"connect_timeout": 5},
        pool_pre_ping=True,
        hide_parameters=True,
    )
    if sqlite:
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


engine = make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)
