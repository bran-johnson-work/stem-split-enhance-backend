"""Tests for the two-role (USER / ADMIN) authorization layer.

Covers the ``require_role`` dependency, the ORM-level role default, and the
backfill migration that assigns USER to legacy rows. Like the rest of the suite
these run with no database: the migration is exercised against a mocked Alembic
``op``.
"""

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.api import dependencies
from app.api.dependencies import get_current_user, require_role
from app.models.user import User, UserRole

# --- require_role (unit) --------------------------------------------------


async def test_require_role_allows_matching_role(admin_user):
    guard = require_role(UserRole.ADMIN)
    assert await guard(current_user=admin_user) is admin_user


async def test_require_role_allows_one_of_several(sample_user):
    guard = require_role(UserRole.USER, UserRole.ADMIN)
    assert await guard(current_user=sample_user) is sample_user


async def test_require_role_forbids_other_role(sample_user):
    guard = require_role(UserRole.ADMIN)
    with pytest.raises(HTTPException) as exc:
        await guard(current_user=sample_user)
    assert exc.value.status_code == 403


async def test_require_role_forbids_legacy_null_role(sample_user):
    # A row that somehow predates the backfill (role is None) is not an admin.
    sample_user.role = None
    guard = require_role(UserRole.ADMIN)
    with pytest.raises(HTTPException) as exc:
        await guard(current_user=sample_user)
    assert exc.value.status_code == 403


# --- require_role (HTTP, full dependency chain) ---------------------------


def _app_with_admin_route() -> FastAPI:
    """A minimal app exposing one ADMIN-guarded route to exercise the guard."""
    test_app = FastAPI()

    @test_app.get("/admin-only")
    async def _admin_only(user: User = Depends(require_role(UserRole.ADMIN))):
        return {"ok": True, "role": user.role.value}

    return test_app


async def _call_admin_route(test_app: FastAPI) -> int:
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.get("/admin-only")
    return resp.status_code


async def test_admin_route_forbidden_for_user(sample_user):
    test_app = _app_with_admin_route()
    test_app.dependency_overrides[get_current_user] = lambda: sample_user
    assert await _call_admin_route(test_app) == 403


async def test_admin_route_allowed_for_admin(admin_user):
    test_app = _app_with_admin_route()
    test_app.dependency_overrides[get_current_user] = lambda: admin_user
    assert await _call_admin_route(test_app) == 200


# --- role default (ORM) ---------------------------------------------------


def test_user_role_column_defaults_to_user():
    # An existing/new user with no explicit role gets USER at insert time.
    assert User.__table__.c.role.default.arg is UserRole.USER
    assert User.__table__.c.role.nullable is False


# --- backfill migration ---------------------------------------------------


def _load_migration():
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "a3f7c1b9d2e5_add_role_to_users.py"
    )
    spec = importlib.util.spec_from_file_location("mig_add_role", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backfill_migration_assigns_user_to_legacy_rows():
    module = _load_migration()
    mock_op = MagicMock()
    mock_op.get_bind.return_value = MagicMock()
    module.op = mock_op

    module.upgrade()

    # The added column is NOT NULL with a server_default of USER, so every row
    # that existed at add time is backfilled to a valid role.
    (table, column), _ = mock_op.add_column.call_args
    assert table == "users"
    assert column.name == "role"
    assert column.nullable is False
    assert "USER" in str(column.server_default.arg)

    # And an explicit, idempotent UPDATE guards any remaining null rows.
    executed = " ".join(str(c.args[0]) for c in mock_op.execute.call_args_list)
    assert "UPDATE users SET role = 'USER'" in executed
    assert "WHERE role IS NULL" in executed


def test_migration_is_chained_on_current_head():
    module = _load_migration()
    assert module.down_revision == "f1a2b3c4d5e6"
    assert module.revision == "a3f7c1b9d2e5"


# --- current_user carries role through (regression) -----------------------


async def test_current_user_exposes_role(monkeypatch, fake_db, admin_user):
    monkeypatch.setattr(
        dependencies.revoked_token_repository, "is_revoked", AsyncMock(return_value=False)
    )
    monkeypatch.setattr(dependencies.user_repository, "get", AsyncMock(return_value=admin_user))
    user = await dependencies.get_current_user(
        db=fake_db, payload={"sub": str(admin_user.id), "jti": "j"}
    )
    assert user.role is UserRole.ADMIN
