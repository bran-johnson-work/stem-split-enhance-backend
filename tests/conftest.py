"""Shared fixtures.

The whole suite runs without a database or network: the async session is a
mock, and service/repository calls are patched per-test. This keeps the tests
fast and deterministic while still exercising the real code paths.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.database import get_db
from app.main import app
from app.models.user import User


@pytest.fixture
def fake_db() -> MagicMock:
    """A stand-in AsyncSession where the async methods are awaitable mocks."""
    db = MagicMock()
    db.execute = AsyncMock()
    db.flush = AsyncMock()
    db.refresh = AsyncMock()
    db.delete = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()
    return db


@pytest.fixture
def sample_user() -> User:
    """A fully-populated User ORM instance (not attached to any session)."""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return User(
        id=uuid.uuid4(),
        email="user@example.com",
        password_hash="salt:hash",
        full_name="Test User",
        is_active=True,
        is_verified=False,
        created_at=now,
        updated_at=now,
    )


def _execute_result(value):
    """Build a mock Result whose scalars().first()/all() return ``value``."""
    result = MagicMock()
    result.scalars.return_value.first.return_value = value
    result.scalars.return_value.all.return_value = value
    return result


@pytest.fixture
def execute_result():
    return _execute_result


@pytest_asyncio.fixture
async def client(fake_db) -> AsyncClient:
    """HTTP client bound to the ASGI app with get_db overridden to fake_db."""

    async def override_get_db():
        yield fake_db

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
