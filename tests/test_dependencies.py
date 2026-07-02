import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import dependencies
from app.core.security import create_access_token


async def test_returns_user_for_valid_token(monkeypatch, fake_db, sample_user):
    monkeypatch.setattr(dependencies.user_repository, "get", AsyncMock(return_value=sample_user))
    token = create_access_token({"sub": str(sample_user.id)})
    user = await dependencies.get_current_user(db=fake_db, token=token)
    assert user is sample_user


async def test_rejects_token_without_sub(fake_db):
    token = create_access_token({"foo": "bar"})
    with pytest.raises(HTTPException) as exc:
        await dependencies.get_current_user(db=fake_db, token=token)
    assert exc.value.status_code == 401


async def test_rejects_garbage_token(fake_db):
    with pytest.raises(HTTPException) as exc:
        await dependencies.get_current_user(db=fake_db, token="not.a.jwt")
    assert exc.value.status_code == 401


async def test_rejects_non_uuid_subject(fake_db):
    token = create_access_token({"sub": "not-a-uuid"})
    with pytest.raises(HTTPException) as exc:
        await dependencies.get_current_user(db=fake_db, token=token)
    assert exc.value.status_code == 401


async def test_rejects_when_user_not_found(monkeypatch, fake_db):
    monkeypatch.setattr(dependencies.user_repository, "get", AsyncMock(return_value=None))
    token = create_access_token({"sub": str(uuid.uuid4())})
    with pytest.raises(HTTPException) as exc:
        await dependencies.get_current_user(db=fake_db, token=token)
    assert exc.value.status_code == 401
