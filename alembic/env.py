"""Alembic environment for Argosy.

Uses the project's async SQLAlchemy URL (sqlite+aiosqlite). For Alembic
itself, we run migrations via `engine.begin().run_sync(...)` because
Alembic's `context.run_migrations()` is sync.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from argosy.config import get_settings
from argosy.state.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Database URL. Prefer an EXPLICIT override so a migration can target a chosen
# DB (a test/temp copy) instead of ALWAYS the live production book. Priority:
#   1. `alembic -x db_url=sqlite+aiosqlite:///path`   (per-invocation)
#   2. env  ARGOSY_ALEMBIC_URL                        (per-shell)
#   3. settings.database_url                          (default = live)
# Before this, env.py unconditionally used the live URL and ignored every
# override, so `alembic upgrade head` silently ran against the ~$4.2M live
# book regardless of intent (the 0098 spine-migration incident, 2026-08). The
# override should use the +aiosqlite driver for online mode.
settings = get_settings()
_x_args = context.get_x_argument(as_dictionary=True)
_override_url = _x_args.get("db_url") or os.environ.get("ARGOSY_ALEMBIC_URL")
config.set_main_option("sqlalchemy.url", _override_url or settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Generate SQL migration scripts without a live DB."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # SQLite ALTER limitations
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
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
