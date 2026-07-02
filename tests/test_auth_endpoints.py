from unittest.mock import AsyncMock

from app.api.v1.endpoints import auth as auth_module


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
    assert "password_hash" not in body  # response schema never leaks the hash


async def test_register_duplicate_returns_400(client, monkeypatch):
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
    assert "already registered" in resp.json()["detail"]


async def test_register_validation_error_returns_422(client):
    resp = await client.post("/api/v1/auth/register", json={"email": "user@example.com"})
    assert resp.status_code == 422  # missing required 'password'


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


async def test_login_inactive_user_returns_400(client, monkeypatch, sample_user):
    sample_user.is_active = False
    monkeypatch.setattr(
        auth_module.user_service, "authenticate", AsyncMock(return_value=sample_user)
    )
    resp = await client.post(
        "/api/v1/auth/login",
        data={"username": sample_user.email, "password": "secret123"},
    )
    assert resp.status_code == 400
    assert "Inactive" in resp.json()["detail"]
