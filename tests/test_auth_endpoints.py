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
