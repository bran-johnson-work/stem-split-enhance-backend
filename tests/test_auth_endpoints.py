from unittest.mock import AsyncMock

import pytest

from app.api.v1.endpoints import auth as auth_module


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Isolate the module-level login throttle between tests."""
    auth_module.login_rate_limiter.clear()
    yield
    auth_module.login_rate_limiter.clear()


async def test_register_returns_201(client, monkeypatch, sample_user):
    monkeypatch.setattr(
        auth_module.user_service, "create_user", AsyncMock(return_value=sample_user)
    )
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "user@example.com", "password": "secret123", "full_name": "Test User"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["email"] == sample_user.email
    assert "password_hash" not in body


async def test_register_duplicate_returns_generic_400(client, monkeypatch):
    monkeypatch.setattr(
        auth_module.user_service,
        "create_user",
        AsyncMock(side_effect=ValueError("Email already registered")),
    )
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "user@example.com", "password": "secret123"},
    )
    assert resp.status_code == 400
    # Must NOT leak whether the email exists.
    assert "already registered" not in resp.json()["detail"].lower()


async def test_register_validation_error_returns_422(client):
    resp = await client.post("/api/v1/auth/register", json={"email": "user@example.com"})
    assert resp.status_code == 422


async def test_login_returns_token(client, monkeypatch, sample_user):
    sample_user.is_active = True
    sample_user.is_verified = True
    monkeypatch.setattr(
        auth_module.user_service, "authenticate", AsyncMock(return_value=sample_user)
    )
    resp = await client.post(
        "/api/v1/auth/login",
        data={"username": sample_user.email, "password": "secret123"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]


async def test_login_unverified_user_returns_403(client, monkeypatch, sample_user):
    sample_user.is_active = True
    sample_user.is_verified = False
    monkeypatch.setattr(
        auth_module.user_service, "authenticate", AsyncMock(return_value=sample_user)
    )
    resp = await client.post(
        "/api/v1/auth/login",
        data={"username": sample_user.email, "password": "secret123"},
    )
    # Correct credentials but unverified email -> blocked with a distinct 403.
    assert resp.status_code == 403
    assert "verify" in resp.json()["detail"].lower()


async def test_login_bad_credentials_returns_401(client, monkeypatch):
    monkeypatch.setattr(auth_module.user_service, "authenticate", AsyncMock(return_value=None))
    resp = await client.post(
        "/api/v1/auth/login",
        data={"username": "user@example.com", "password": "wrong"},
    )
    assert resp.status_code == 401


async def test_login_inactive_user_is_indistinguishable_from_bad_creds(
    client, monkeypatch, sample_user
):
    sample_user.is_active = False
    monkeypatch.setattr(
        auth_module.user_service, "authenticate", AsyncMock(return_value=sample_user)
    )
    resp = await client.post(
        "/api/v1/auth/login",
        data={"username": sample_user.email, "password": "secret123"},
    )
    # Inactive users get the SAME 401 + message as bad credentials (no enumeration).
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Incorrect email or password"


async def test_login_is_rate_limited_after_repeated_failures(client, monkeypatch):
    monkeypatch.setattr(auth_module.user_service, "authenticate", AsyncMock(return_value=None))
    limit = auth_module.settings.login_rate_limit_max_attempts
    # Exhaust the allowed attempts.
    for _ in range(limit):
        r = await client.post(
            "/api/v1/auth/login", data={"username": "victim@example.com", "password": "x"}
        )
        assert r.status_code == 401
    # The next attempt is throttled.
    blocked = await client.post(
        "/api/v1/auth/login", data={"username": "victim@example.com", "password": "x"}
    )
    assert blocked.status_code == 429


async def test_logout_revokes_token(client, monkeypatch, sample_user):
    # Authenticate to get a real token.
    sample_user.is_active = True
    sample_user.is_verified = True
    monkeypatch.setattr(
        auth_module.user_service, "authenticate", AsyncMock(return_value=sample_user)
    )
    login = await client.post(
        "/api/v1/auth/login", data={"username": sample_user.email, "password": "secret123"}
    )
    token = login.json()["access_token"]

    revoke = AsyncMock()
    monkeypatch.setattr(auth_module.auth_service, "logout", revoke)
    resp = await client.post(
        "/api/v1/auth/logout", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    assert resp.json()["detail"] == "Successfully logged out"
    revoke.assert_awaited_once()


async def test_logout_requires_valid_token(client):
    resp = await client.post("/api/v1/auth/logout")
    assert resp.status_code == 401


async def test_register_triggers_verification_email(client, monkeypatch, sample_user):
    monkeypatch.setattr(
        auth_module.user_service, "create_user", AsyncMock(return_value=sample_user)
    )
    send = AsyncMock(return_value="msg_1")
    monkeypatch.setattr(auth_module.email_service, "send_verification_email", send)

    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "user@example.com", "password": "secret123"},
    )
    assert resp.status_code == 201
    # Background task runs within the request lifecycle under ASGITransport.
    send.assert_awaited_once_with(sample_user)


async def test_verify_email_request_sends_for_unverified_user(client, monkeypatch, sample_user):
    sample_user.is_verified = False
    monkeypatch.setattr(
        auth_module.user_service.repository, "get_by_email", AsyncMock(return_value=sample_user)
    )
    send = AsyncMock(return_value="msg_1")
    monkeypatch.setattr(auth_module.email_service, "send_verification_email", send)

    resp = await client.post(
        "/api/v1/auth/verify-email/request", json={"email": sample_user.email}
    )
    assert resp.status_code == 200
    send.assert_awaited_once_with(sample_user)


async def test_verify_email_request_is_generic_for_unknown_email(client, monkeypatch):
    monkeypatch.setattr(
        auth_module.user_service.repository, "get_by_email", AsyncMock(return_value=None)
    )
    send = AsyncMock()
    monkeypatch.setattr(auth_module.email_service, "send_verification_email", send)

    resp = await client.post(
        "/api/v1/auth/verify-email/request", json={"email": "ghost@example.com"}
    )
    # Same 200 + message whether or not the account exists (no enumeration).
    assert resp.status_code == 200
    assert "if an account exists" in resp.json()["detail"].lower()
    send.assert_not_awaited()


async def test_verify_email_confirm_success(client, monkeypatch, sample_user):
    monkeypatch.setattr(
        auth_module.email_service, "verify_email_token", AsyncMock(return_value=sample_user)
    )
    resp = await client.post(
        "/api/v1/auth/verify-email/confirm", json={"token": "valid-token"}
    )
    assert resp.status_code == 200
    assert "verified" in resp.json()["detail"].lower()


async def test_verify_email_confirm_invalid_token_returns_400(client, monkeypatch):
    monkeypatch.setattr(
        auth_module.email_service, "verify_email_token", AsyncMock(return_value=None)
    )
    resp = await client.post(
        "/api/v1/auth/verify-email/confirm", json={"token": "bad-token"}
    )
    assert resp.status_code == 400
