from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

settings = get_settings()

# Connection URL and driver args both come from Settings so the app, the worker,
# and Alembic stay in lockstep: discrete POSTGRES_* components (AWS RDS) with an
# optional TLS handshake, or the legacy Supabase pooler with its prepared-statement
# workaround. See Settings.active_database_url / asyncpg_connect_args.
engine = create_async_engine(
    settings.active_database_url,
    pool_pre_ping=True,
    echo=settings.db_echo,
    connect_args=settings.asyncpg_connect_args(),
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session
