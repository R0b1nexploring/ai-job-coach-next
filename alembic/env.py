from __future__ import annotations

from logging.config import fileConfig
from uuid import uuid4

from alembic import context
from sqlalchemy import engine_from_config, pool, text

from app.db import Base, database_url
from app import models  # noqa: F401

config = context.config
if config.config_file_name and config.get_section(config.config_ini_section, {}).get("loggers"):
    fileConfig(config.config_file_name)
config.set_main_option("sqlalchemy.url", database_url().replace("%", "%%"))
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section), prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            compatibility_schema = None
            original_search_path = None
            if connection.dialect.name == "postgresql":
                # 0002 was already used by the SQLite prototype. Keep it unchanged
                # while supporting its randomblob/hex backfill on PostgreSQL.
                # The private schema only exists inside this migration transaction.
                compatibility_schema = "coach_migration_" + uuid4().hex
                original_search_path = connection.scalar(text("SHOW search_path"))
                connection.execute(text(f'CREATE SCHEMA "{compatibility_schema}"'))
                connection.execute(text(f'''CREATE FUNCTION "{compatibility_schema}".randomblob(integer)
                    RETURNS bytea LANGUAGE SQL AS $$
                    SELECT decode(replace(gen_random_uuid()::text, '-', ''), 'hex')
                    $$'''))
                connection.execute(text(f'''CREATE FUNCTION "{compatibility_schema}".hex(bytea)
                    RETURNS text LANGUAGE SQL AS $$ SELECT encode($1, 'hex') $$'''))
                connection.execute(text("SELECT set_config('search_path', :path, true)"), {"path": original_search_path + ', "' + compatibility_schema + '"'})
            context.run_migrations()
            if compatibility_schema:
                connection.execute(text("SELECT set_config('search_path', :path, true)"), {"path": original_search_path})
                connection.execute(text(f'DROP SCHEMA "{compatibility_schema}" CASCADE'))


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
