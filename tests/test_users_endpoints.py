import uuid
from unittest.mock import AsyncMock

import pytest

from app.api.dependencies import get_current_user
from app.api.v1.endpoints import users as users_module
from app.main import app


@pytest.fixture
def auth_client(client, sample_user):
    """The client fixture, with get_current_user forced to sample_user."""
    app.dependency_overrides[get_current_user] = lambda: sample_user
    yield client
    app.dependency_overrides.pop(get_current_user, None)


async def test_read_me(auth_client, sample_user):
    resp = await auth_client.get("/api/v1/users/me")
    assert resp.status_code == 200
    assert resp.json()["email"] == sample_user.email


async def test_read_user_found(auth_client, monkeypatch, sample_user):
    monkeypatch.setattr(
        users_module.user_service.repository, "get", AsyncMock(return_value=sample_user)
    )
    resp = await auth_client.get(f"/api/v1/users/{sample_user.id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == str(sample_user.id)


async def test_read_other_user_forbidden(auth_client):
    # A user may only read their own record; another id is rejected (no IDOR).
    resp = await auth_client.get(f"/api/v1/users/{uuid.uuid4()}")
    assert resp.status_code == 403


async def test_read_own_user_missing_returns_404(auth_client, monkeypatch, sample_user):
    # Reading self, but the record no longer resolves -> 404 (not 403).
    monkeypatch.setattr(users_module.user_service.repository, "get", AsyncMock(return_value=None))
    resp = await auth_client.get(f"/api/v1/users/{sample_user.id}")
    assert resp.status_code == 404


async def test_update_user_forbidden_for_other_user(auth_client):
    resp = await auth_client.patch(f"/api/v1/users/{uuid.uuid4()}", json={"full_name": "New"})
    assert resp.status_code == 403


async def test_update_user_cannot_self_verify(auth_client, monkeypatch, sample_user):
    # Mass-assignment guard: is_verified/is_active are not fields on UserUpdate,
    # so even if a client sends them the service never receives them and cannot
    # flip server-controlled flags.
    captured = {}

    async def _capture(db, *, user_id, user_in):
        captured["data"] = user_in.model_dump(exclude_unset=True)
        return sample_user

    monkeypatch.setattr(users_module.user_service, "update_user", _capture)
    resp = await auth_client.patch(
        f"/api/v1/users/{sample_user.id}",
        json={"is_verified": True, "is_active": True, "full_name": "Legit"},
    )
    assert resp.status_code == 200
    assert "is_verified" not in captured["data"]
    assert "is_active" not in captured["data"]
    assert captured["data"] == {"full_name": "Legit"}


async def test_update_user_duplicate_email_returns_409(auth_client, monkeypatch, sample_user):
    monkeypatch.setattr(
        users_module.user_service,
        "update_user",
        AsyncMock(side_effect=ValueError("Email already registered")),
    )
    resp = await auth_client.patch(
        f"/api/v1/users/{sample_user.id}", json={"email": "taken@example.com"}
    )
    assert resp.status_code == 409


async def test_update_user_success(auth_client, monkeypatch, sample_user):
    monkeypatch.setattr(
        users_module.user_service, "update_user", AsyncMock(return_value=sample_user)
    )
    resp = await auth_client.patch(
        f"/api/v1/users/{sample_user.id}", json={"full_name": "Renamed"}
    )
    assert resp.status_code == 200


async def test_update_user_not_found(auth_client, monkeypatch, sample_user):
    monkeypatch.setattr(users_module.user_service, "update_user", AsyncMock(return_value=None))
    resp = await auth_client.patch(
        f"/api/v1/users/{sample_user.id}", json={"full_name": "Renamed"}
    )
    assert resp.status_code == 404


async def test_delete_user_forbidden_for_other_user(auth_client):
    resp = await auth_client.delete(f"/api/v1/users/{uuid.uuid4()}")
    assert resp.status_code == 403


async def test_delete_user_success(auth_client, monkeypatch, sample_user, fake_db):
    monkeypatch.setattr(
        users_module.user_service.repository, "delete", AsyncMock(return_value=True)
    )
    resp = await auth_client.delete(f"/api/v1/users/{sample_user.id}")
    assert resp.status_code == 204
    fake_db.commit.assert_awaited_once()


async def test_delete_user_not_found(auth_client, monkeypatch, sample_user):
    monkeypatch.setattr(
        users_module.user_service.repository, "delete", AsyncMock(return_value=False)
    )
    resp = await auth_client.delete(f"/api/v1/users/{sample_user.id}")
    assert resp.status_code == 404


async def test_protected_route_without_auth_returns_401(client):
    # No get_current_user override here → real dependency rejects the missing token.
    resp = await client.get("/api/v1/users/me")
    assert resp.status_code == 401
