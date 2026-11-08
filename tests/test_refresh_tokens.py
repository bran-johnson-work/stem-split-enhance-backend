"""Refresh-token rotation, expiry and reuse detection.

Like the rest of the suite these run with no database: the repository is
patched per-test, so what is exercised is ``AuthService``'s decision logic —
which token is accepted, what gets revoked, and what the caller is told.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.core.security import create_refresh_token, hash_refresh_token
from app.models.refresh_token import RefreshToken
from app.models.user import User, UserRole
from app.services.auth_service import RefreshError, auth_service


def make_user(*, is_active: bool = True) -> User:
    return User(
        id=uuid.uuid4(),
        email="user@example.com",
        password_hash="x",
        is_active=is_active,
        is_verified=True,
        role=UserRole.USER,
    )


def make_record(
    user_id: uuid.UUID,
    *,
    token_hash: str,
    revoked: bool = False,
    expires_in_days: float = 30,
) -> RefreshToken:
    record = RefreshToken(
        id=uuid.uuid4(),
        user_id=user_id,
        token_hash=token_hash,
        expires_at=datetime.now(timezone.utc) + timedelta(days=expires_in_days),
        revoked_at=datetime.now(timezone.utc) if revoked else None,
    )
    return record


# ---- token primitives -----------------------------------------------------


def test_refresh_tokens_are_unique_and_opaque():
    tokens = {create_refresh_token() for _ in range(100)}
    assert len(tokens) == 100, "CSPRNG output must not collide"
    # Opaque, not a JWT — nothing should be able to read claims out of it.
    assert all("." not in t for t in tokens)


def test_hash_is_stable_and_not_the_token():
    token = create_refresh_token()
    assert hash_refresh_token(token) == hash_refresh_token(token)
    assert hash_refresh_token(token) != token
    assert len(hash_refresh_token(token)) == 64


# ---- rotation -------------------------------------------------------------


async def test_rotate_issues_new_token_and_revokes_the_old(fake_db, monkeypatch):
    user = make_user()
    token = create_refresh_token()
    record = make_record(user.id, token_hash=hash_refresh_token(token))
    successor = make_record(user.id, token_hash="successor-hash")

    monkeypatch.setattr(
        auth_service.refresh_tokens, "get_by_hash", AsyncMock(return_value=record)
    )
    monkeypatch.setattr(
        auth_service.refresh_tokens, "create", AsyncMock(return_value=successor)
    )
    revoke = AsyncMock()
    monkeypatch.setattr(auth_service.refresh_tokens, "revoke", revoke)
    monkeypatch.setattr(auth_service.users, "get", AsyncMock(return_value=user))

    returned_user, new_token = await auth_service.rotate_refresh_token(fake_db, token)

    assert returned_user is user
    assert new_token != token, "rotation must hand back a different token"
    # The presented token is retired and linked to what replaced it.
    revoke.assert_awaited_once()
    assert revoke.call_args.args[1] is record
    assert revoke.call_args.kwargs["replaced_by_id"] == successor.id
    fake_db.commit.assert_awaited()


async def test_unknown_token_is_rejected(fake_db, monkeypatch):
    monkeypatch.setattr(
        auth_service.refresh_tokens, "get_by_hash", AsyncMock(return_value=None)
    )

    with pytest.raises(RefreshError):
        await auth_service.rotate_refresh_token(fake_db, create_refresh_token())


async def test_expired_token_is_rejected(fake_db, monkeypatch):
    user = make_user()
    token = create_refresh_token()
    record = make_record(
        user.id, token_hash=hash_refresh_token(token), expires_in_days=-1
    )
    monkeypatch.setattr(
        auth_service.refresh_tokens, "get_by_hash", AsyncMock(return_value=record)
    )

    with pytest.raises(RefreshError):
        await auth_service.rotate_refresh_token(fake_db, token)


async def test_naive_expiry_does_not_crash(fake_db, monkeypatch):
    """A tz-naive ``expires_at`` must be treated as UTC, not raise."""
    user = make_user()
    token = create_refresh_token()
    record = make_record(user.id, token_hash=hash_refresh_token(token))
    record.expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1)

    monkeypatch.setattr(
        auth_service.refresh_tokens, "get_by_hash", AsyncMock(return_value=record)
    )

    with pytest.raises(RefreshError):
        await auth_service.rotate_refresh_token(fake_db, token)


async def test_deactivated_account_cannot_refresh(fake_db, monkeypatch):
    user = make_user(is_active=False)
    token = create_refresh_token()
    record = make_record(user.id, token_hash=hash_refresh_token(token))

    monkeypatch.setattr(
        auth_service.refresh_tokens, "get_by_hash", AsyncMock(return_value=record)
    )
    monkeypatch.setattr(auth_service.users, "get", AsyncMock(return_value=user))
    revoke = AsyncMock()
    monkeypatch.setattr(auth_service.refresh_tokens, "revoke", revoke)

    with pytest.raises(RefreshError):
        await auth_service.rotate_refresh_token(fake_db, token)
    revoke.assert_awaited_once()


# ---- reuse detection ------------------------------------------------------


async def test_replaying_a_used_token_revokes_the_whole_family(fake_db, monkeypatch):
    """The security property that makes rotation worth having.

    Presenting an already-rotated token means someone has a copy they shouldn't.
    We cannot tell the thief from the victim, so every live session dies.
    """
    user = make_user()
    token = create_refresh_token()
    record = make_record(user.id, token_hash=hash_refresh_token(token), revoked=True)

    monkeypatch.setattr(
        auth_service.refresh_tokens, "get_by_hash", AsyncMock(return_value=record)
    )
    revoke_all = AsyncMock(return_value=3)
    monkeypatch.setattr(
        auth_service.refresh_tokens, "revoke_all_for_user", revoke_all
    )
    # Must never reach token minting.
    monkeypatch.setattr(
        auth_service.refresh_tokens,
        "create",
        AsyncMock(side_effect=AssertionError("must not mint a token on replay")),
    )

    with pytest.raises(RefreshError):
        await auth_service.rotate_refresh_token(fake_db, token)

    revoke_all.assert_awaited_once_with(fake_db, record.user_id)
    fake_db.commit.assert_awaited()


# ---- issue / revoke -------------------------------------------------------


async def test_issue_stores_only_the_hash(fake_db, monkeypatch):
    user_id = uuid.uuid4()
    create = AsyncMock(return_value=make_record(user_id, token_hash="h"))
    monkeypatch.setattr(auth_service.refresh_tokens, "create", create)

    token = await auth_service.issue_refresh_token(fake_db, user_id)

    stored = create.call_args.kwargs["token_hash"]
    assert stored == hash_refresh_token(token)
    assert stored != token, "the raw token must never be persisted"
    # Login owns the transaction — issuing must not commit on its own.
    fake_db.commit.assert_not_awaited()


async def test_issue_sets_expiry_in_the_future(fake_db, monkeypatch):
    create = AsyncMock(return_value=make_record(uuid.uuid4(), token_hash="h"))
    monkeypatch.setattr(auth_service.refresh_tokens, "create", create)

    await auth_service.issue_refresh_token(fake_db, uuid.uuid4())

    assert create.call_args.kwargs["expires_at"] > datetime.now(timezone.utc)


async def test_logout_revokes_a_live_token(fake_db, monkeypatch):
    user = make_user()
    token = create_refresh_token()
    record = make_record(user.id, token_hash=hash_refresh_token(token))

    monkeypatch.setattr(
        auth_service.refresh_tokens, "get_by_hash", AsyncMock(return_value=record)
    )
    revoke = AsyncMock()
    monkeypatch.setattr(auth_service.refresh_tokens, "revoke", revoke)

    await auth_service.revoke_refresh_token(fake_db, token)

    revoke.assert_awaited_once()
    fake_db.commit.assert_awaited_once()


async def test_logout_with_an_unknown_token_is_a_no_op(fake_db, monkeypatch):
    """Logging out must succeed even if the client sends a stale token."""
    monkeypatch.setattr(
        auth_service.refresh_tokens, "get_by_hash", AsyncMock(return_value=None)
    )
    revoke = AsyncMock()
    monkeypatch.setattr(auth_service.refresh_tokens, "revoke", revoke)

    await auth_service.revoke_refresh_token(fake_db, "not-a-real-token")

    revoke.assert_not_awaited()
