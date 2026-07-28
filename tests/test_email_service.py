"""Tests for the email service: message composition and the verification flow."""

from unittest.mock import AsyncMock

from app.core.email import EmailError
from app.core.security import (
    create_access_token,
    create_email_verification_token,
    create_password_reset_token,
)
from app.services import email_service as email_service_module
from app.services.email_service import email_service


async def test_send_verification_email_composes_and_sends(monkeypatch, sample_user):
    send = AsyncMock(return_value="msg_1")
    monkeypatch.setattr(email_service_module, "send_email", send)

    result = await email_service.send_verification_email(sample_user)

    assert result == "msg_1"
    send.assert_awaited_once()
    kwargs = send.call_args.kwargs
    assert kwargs["to"] == sample_user.email
    assert "email" in kwargs["subject"].lower()
    # The verification link (frontend URL + token) must be embedded in the body.
    assert "verify-email?token=" in kwargs["html"]


async def test_send_verification_email_swallows_send_failure(monkeypatch, sample_user):
    monkeypatch.setattr(
        email_service_module, "send_email", AsyncMock(side_effect=EmailError("down"))
    )
    # Must not propagate — a failed email can't break registration.
    assert await email_service.send_verification_email(sample_user) is None


def _allow_fresh_token(monkeypatch):
    """Make the single-use denylist report the token as not-yet-consumed."""
    monkeypatch.setattr(
        email_service.revoked_tokens, "is_revoked", AsyncMock(return_value=False)
    )
    add = AsyncMock()
    monkeypatch.setattr(email_service.revoked_tokens, "add", add)
    return add


async def test_verify_email_token_marks_verified_and_consumes_token(
    monkeypatch, fake_db, sample_user
):
    sample_user.is_verified = False
    token = create_email_verification_token(str(sample_user.id), sample_user.email)

    consume = _allow_fresh_token(monkeypatch)
    monkeypatch.setattr(email_service.repository, "get", AsyncMock(return_value=sample_user))
    update = AsyncMock(return_value=sample_user)
    monkeypatch.setattr(email_service.repository, "update", update)

    user = await email_service.verify_email_token(fake_db, token)

    assert user is sample_user
    update.assert_awaited_once()
    assert update.call_args.args[2] == {"is_verified": True}
    consume.assert_awaited_once()  # jti denylisted (single-use)
    fake_db.commit.assert_awaited_once()


async def test_verify_email_token_rejects_reused_token(monkeypatch, fake_db, sample_user):
    token = create_email_verification_token(str(sample_user.id), sample_user.email)
    # Denylist reports the jti as already consumed.
    monkeypatch.setattr(
        email_service.revoked_tokens, "is_revoked", AsyncMock(return_value=True)
    )
    get = AsyncMock(return_value=sample_user)
    monkeypatch.setattr(email_service.repository, "get", get)

    assert await email_service.verify_email_token(fake_db, token) is None
    get.assert_not_awaited()  # short-circuits before touching the user
    fake_db.commit.assert_not_awaited()


async def test_verify_email_token_idempotent_when_already_verified(
    monkeypatch, fake_db, sample_user
):
    sample_user.is_verified = True
    token = create_email_verification_token(str(sample_user.id), sample_user.email)

    consume = _allow_fresh_token(monkeypatch)
    monkeypatch.setattr(email_service.repository, "get", AsyncMock(return_value=sample_user))
    update = AsyncMock()
    monkeypatch.setattr(email_service.repository, "update", update)

    user = await email_service.verify_email_token(fake_db, token)

    assert user is sample_user
    update.assert_not_awaited()  # flag already set — no re-write
    consume.assert_awaited_once()  # but the token is still consumed
    fake_db.commit.assert_awaited_once()


async def test_verify_email_token_rejects_invalid_token(fake_db):
    assert await email_service.verify_email_token(fake_db, "not-a-jwt") is None


async def test_verify_email_token_rejects_access_token(fake_db, sample_user):
    # An ordinary access token lacks the email-verification purpose claim.
    access = create_access_token(data={"sub": str(sample_user.id)})
    assert await email_service.verify_email_token(fake_db, access) is None


async def test_verify_email_token_rejects_email_mismatch(monkeypatch, fake_db, sample_user):
    # Token minted for an old address; the user has since changed it.
    token = create_email_verification_token(str(sample_user.id), "old@example.com")
    _allow_fresh_token(monkeypatch)
    monkeypatch.setattr(email_service.repository, "get", AsyncMock(return_value=sample_user))
    assert await email_service.verify_email_token(fake_db, token) is None


async def test_verify_email_token_returns_none_for_missing_user(monkeypatch, fake_db, sample_user):
    token = create_email_verification_token(str(sample_user.id), sample_user.email)
    _allow_fresh_token(monkeypatch)
    monkeypatch.setattr(email_service.repository, "get", AsyncMock(return_value=None))
    assert await email_service.verify_email_token(fake_db, token) is None


# --- Password reset -----------------------------------------------------------


async def test_send_password_reset_email_composes_and_sends(monkeypatch, sample_user):
    send = AsyncMock(return_value="msg_2")
    monkeypatch.setattr(email_service_module, "send_email", send)

    result = await email_service.send_password_reset_email(sample_user)

    assert result == "msg_2"
    send.assert_awaited_once()
    kwargs = send.call_args.kwargs
    assert kwargs["to"] == sample_user.email
    assert "password" in kwargs["subject"].lower()
    # The reset link (frontend URL + token) must be embedded in the body.
    assert "reset-password?token=" in kwargs["html"]


async def test_send_password_reset_email_swallows_send_failure(monkeypatch, sample_user):
    monkeypatch.setattr(
        email_service_module, "send_email", AsyncMock(side_effect=EmailError("down"))
    )
    # Must not propagate — a failed email can't break the (generic) endpoint.
    assert await email_service.send_password_reset_email(sample_user) is None


async def test_reset_password_sets_hash_and_revokes_sessions(monkeypatch, fake_db, sample_user):
    sample_user.is_verified = True
    token = create_password_reset_token(str(sample_user.id), sample_user.email)

    consume = _allow_fresh_token(monkeypatch)
    monkeypatch.setattr(email_service.repository, "get", AsyncMock(return_value=sample_user))
    update = AsyncMock(return_value=sample_user)
    monkeypatch.setattr(email_service.repository, "update", update)
    revoke_all = AsyncMock(return_value=2)
    monkeypatch.setattr(email_service.refresh_tokens, "revoke_all_for_user", revoke_all)

    user = await email_service.reset_password(fake_db, token, "new-password-123")

    assert user is sample_user
    # A fresh password hash was written (never the plaintext).
    update.assert_awaited_once()
    written = update.call_args.args[2]
    assert "password_hash" in written
    assert written["password_hash"].startswith("pbkdf2_sha256$")
    assert "new-password-123" not in written["password_hash"]
    consume.assert_awaited_once()  # jti denylisted (single-use)
    revoke_all.assert_awaited_once_with(fake_db, sample_user.id)  # sign out everywhere
    fake_db.commit.assert_awaited_once()


async def test_reset_password_also_verifies_unverified_user(monkeypatch, fake_db, sample_user):
    # Completing a reset over an emailed link proves email ownership.
    sample_user.is_verified = False
    token = create_password_reset_token(str(sample_user.id), sample_user.email)
    _allow_fresh_token(monkeypatch)
    monkeypatch.setattr(email_service.repository, "get", AsyncMock(return_value=sample_user))
    update = AsyncMock(return_value=sample_user)
    monkeypatch.setattr(email_service.repository, "update", update)
    monkeypatch.setattr(
        email_service.refresh_tokens, "revoke_all_for_user", AsyncMock(return_value=0)
    )

    await email_service.reset_password(fake_db, token, "new-password-123")

    assert update.call_args.args[2].get("is_verified") is True


async def test_reset_password_rejects_reused_token(monkeypatch, fake_db, sample_user):
    token = create_password_reset_token(str(sample_user.id), sample_user.email)
    monkeypatch.setattr(
        email_service.revoked_tokens, "is_revoked", AsyncMock(return_value=True)
    )
    get = AsyncMock(return_value=sample_user)
    monkeypatch.setattr(email_service.repository, "get", get)

    assert await email_service.reset_password(fake_db, token, "new-password-123") is None
    get.assert_not_awaited()  # short-circuits before touching the user
    fake_db.commit.assert_not_awaited()


async def test_reset_password_rejects_invalid_token(fake_db):
    assert await email_service.reset_password(fake_db, "not-a-jwt", "new-password-123") is None


async def test_reset_password_rejects_access_token(fake_db, sample_user):
    access = create_access_token(data={"sub": str(sample_user.id)})
    assert await email_service.reset_password(fake_db, access, "new-password-123") is None


async def test_reset_password_rejects_inactive_user(monkeypatch, fake_db, sample_user):
    sample_user.is_active = False
    token = create_password_reset_token(str(sample_user.id), sample_user.email)
    _allow_fresh_token(monkeypatch)
    monkeypatch.setattr(email_service.repository, "get", AsyncMock(return_value=sample_user))
    assert await email_service.reset_password(fake_db, token, "new-password-123") is None


async def test_reset_password_rejects_email_mismatch(monkeypatch, fake_db, sample_user):
    # Token minted for an old address; the user has since changed it.
    token = create_password_reset_token(str(sample_user.id), "old@example.com")
    _allow_fresh_token(monkeypatch)
    monkeypatch.setattr(email_service.repository, "get", AsyncMock(return_value=sample_user))
    assert await email_service.reset_password(fake_db, token, "new-password-123") is None
