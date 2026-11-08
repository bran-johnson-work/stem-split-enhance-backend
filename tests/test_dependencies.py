import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.api import dependencies
from app.core.security import create_access_token


@pytest.fixture(autouse=True)
def _no_revocation(monkeypatch):
    """Default: tokens are not revoked. Individual tests override as needed."""
    monkeypatch.setattr(
        dependencies.revoked_token_repository, "is_revoked", AsyncMock(return_value=False)
    )


# --- get_token_payload ----------------------------------------------------


async def test_token_payload_valid():
    token = create_access_token({"sub": str(uuid.uuid4())})
    payload = await dependencies.get_token_payload(token=token)
    assert "jti" in payload and "sub" in payload


async def test_token_payload_rejects_garbage():
    with pytest.raises(HTTPException) as exc:
        await dependencies.get_token_payload(token="not.a.jwt")
    assert exc.value.status_code == 401


async def test_token_payload_rejects_missing_sub():
    token = create_access_token({"foo": "bar"})  # no sub
    with pytest.raises(HTTPException) as exc:
        await dependencies.get_token_payload(token=token)
    assert exc.value.status_code == 401


# --- get_current_user -----------------------------------------------------


async def test_current_user_valid(monkeypatch, fake_db, sample_user):
    monkeypatch.setattr(dependencies.user_repository, "get", AsyncMock(return_value=sample_user))
    payload = {"sub": str(sample_user.id), "jti": "jti-1"}
    user = await dependencies.get_current_user(db=fake_db, payload=payload)
    assert user is sample_user


async def test_current_user_rejects_revoked_token(monkeypatch, fake_db, sample_user):
    monkeypatch.setattr(
        dependencies.revoked_token_repository, "is_revoked", AsyncMock(return_value=True)
    )
    monkeypatch.setattr(dependencies.user_repository, "get", AsyncMock(return_value=sample_user))
    with pytest.raises(HTTPException) as exc:
        await dependencies.get_current_user(
            db=fake_db, payload={"sub": str(sample_user.id), "jti": "revoked"}
        )
    assert exc.value.status_code == 401


async def test_current_user_rejects_non_uuid_sub(fake_db):
    with pytest.raises(HTTPException) as exc:
        await dependencies.get_current_user(db=fake_db, payload={"sub": "not-a-uuid", "jti": "j"})
    assert exc.value.status_code == 401


async def test_current_user_rejects_when_user_missing(monkeypatch, fake_db):
    monkeypatch.setattr(dependencies.user_repository, "get", AsyncMock(return_value=None))
    with pytest.raises(HTTPException) as exc:
        await dependencies.get_current_user(
            db=fake_db, payload={"sub": str(uuid.uuid4()), "jti": "j"}
        )
    assert exc.value.status_code == 401
