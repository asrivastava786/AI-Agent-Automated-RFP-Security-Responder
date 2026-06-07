"""
migrations/env.py – Alembic environment for async Postgres (psycopg v3).

Supports both offline (SQL script generation) and online (live DB) modes.
Online mode uses asyncio so it works with the same psycopg v3 async driver
as the production application.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

# ── Pull DSN from application settings (respects .env file) ──────────────────
from rfp_responder.config import settings

# Alembic Config object
config = context.config

# Override sqlalchemy.url with our Settings DSN.
# Alembic uses SQLAlchemy-style DSNs; convert psycopg v3 scheme if needed.
_dsn = settings.postgres_dsn.replace("postgresql+psycopg://", "postgresql+psycopg://")
config.set_main_option("sqlalchemy.url", _dsn)

# Logging
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata for autogenerate (set to None: we manage migrations manually or
# import your SQLAlchemy Base.metadata here if you add ORM models later)
target_metadata = None


# ─────────────────────────────────────────────────────────────────────────────
# Offline mode – generates a .sql script without a live DB connection
# ─────────────────────────────────────────────────────────────────────────────

def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ─────────────────────────────────────────────────────────────────────────────
# Online mode – runs migrations against a live Postgres instance
# ─────────────────────────────────────────────────────────────────────────────

def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
