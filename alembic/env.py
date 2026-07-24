import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import app.models as models  # noqa: F401 — ensure all models are registered on Base.metadata
from alembic import context
from app.core.config import get_settings
from app.models.base import Base

config = context.config
settings = get_settings()

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Discrete POSTGRES_* components (AWS RDS) take precedence; otherwise fall back to
# the legacy Supabase direct connection. The pooler URL requires
# prepared_statement_cache_size=0 for Alembic DDL compatibility.
_db_url = settings.migration_database_url
if "pooler.supabase.com" in _db_url and "prepared_statement_cache_size" not in _db_url:
    _db_url += "?prepared_statement_cache_size=0"
config.set_main_option("sqlalchemy.url", _db_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    # NullPool: migrations are a short-lived one-shot connection. connect_args carries
    # TLS (postgres_ssl) for RDS; the pooler prepared-statement tweak is already baked
    # into the URL query above for the legacy path.
    connect_args = {"ssl": True} if settings.postgres_ssl else {}
    connectable = create_async_engine(
        config.get_main_option("sqlalchemy.url"),
        poolclass=pool.NullPool,
        connect_args=connect_args,
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
