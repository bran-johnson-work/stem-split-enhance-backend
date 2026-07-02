from collections.abc import AsyncGenerator
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

settings = get_settings()


def _build_connect_args() -> dict:
    """Connection args for the async engine.

    Supabase's transaction pooler (PgBouncer, port 6543) shares a physical
    connection across clients per transaction, so it cannot support the
    server-side prepared statements asyncpg creates by default — reused
    connections collide on names like ``__asyncpg_stmt_1__``. Disable the
    statement caches and give every prepared statement a unique name.
    """
    if "pooler.supabase.com" in settings.active_database_url:
        return {
            "statement_cache_size": 0,
            "prepared_statement_cache_size": 0,
            "prepared_statement_name_func": lambda: f"__asyncpg_{uuid4()}__",
        }
    return {}


engine = create_async_engine(
    settings.active_database_url,
    pool_pre_ping=True,
    echo=settings.is_local,
    connect_args=_build_connect_args(),
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
