import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.security import hash_password, verify_password
from app.schemas.user import UserCreate, UserUpdate
from app.services.user_service import UserService


@pytest.fixture
def service() -> UserService:
    svc = UserService()
    svc.repository = AsyncMock()
    return svc


async def test_create_user_hashes_password_and_commits(service, fake_db, sample_user):
    service.repository.get_by_email.return_value = None
    service.repository.create_user_with_hash.return_value = sample_user

    user_in = UserCreate(email="user@example.com", password="secret123", full_name="Test User")
    result = await service.create_user(fake_db, user_in)

    assert result is sample_user
    service.repository.create_user_with_hash.assert_awaited_once()
    # The stored hash must not be the plaintext password, and must verify.
    passed_hash = service.repository.create_user_with_hash.call_args.kwargs["password_hash"]
    assert passed_hash != "secret123"
    assert verify_password("secret123", passed_hash) is True
    fake_db.commit.assert_awaited_once()


async def test_create_user_rejects_duplicate_email(service, fake_db, sample_user):
    service.repository.get_by_email.return_value = sample_user

    with pytest.raises(ValueError, match="already registered"):
        await service.create_user(
            fake_db, UserCreate(email="user@example.com", password="secret123")
        )
    service.repository.create_user_with_hash.assert_not_called()
    fake_db.commit.assert_not_awaited()


async def test_authenticate_returns_none_for_unknown_email(service, fake_db):
    service.repository.get_by_email.return_value = None
    assert await service.authenticate(fake_db, "nobody@example.com", "pw") is None


async def test_authenticate_returns_none_for_wrong_password(service, fake_db, sample_user):
    sample_user.password_hash = hash_password("correct-horse")
    service.repository.get_by_email.return_value = sample_user
    assert await service.authenticate(fake_db, sample_user.email, "wrong") is None


async def test_authenticate_returns_user_on_success(service, fake_db, sample_user):
    sample_user.password_hash = hash_password("correct-horse")
    service.repository.get_by_email.return_value = sample_user
    assert await service.authenticate(fake_db, sample_user.email, "correct-horse") is sample_user


async def test_authenticate_rejects_passwordless_oauth_account(service, fake_db, sample_user):
    # OAuth2/social account: no local password. No submitted password — empty or
    # otherwise — may authenticate it.
    sample_user.password_hash = None
    service.repository.get_by_email.return_value = sample_user
    assert await service.authenticate(fake_db, sample_user.email, "") is None
    assert await service.authenticate(fake_db, sample_user.email, "anything") is None


async def test_update_user_rejects_duplicate_email(service, fake_db, sample_user):
    other = SimpleNamespace(id=uuid.uuid4())
    service.repository.get.return_value = sample_user
    service.repository.get_by_email.return_value = other

    with pytest.raises(ValueError, match="already registered"):
        await service.update_user(
            fake_db, sample_user.id, UserUpdate(email="taken@example.com")
        )
    service.repository.update.assert_not_called()


async def test_update_user_returns_none_when_missing(service, fake_db):
    service.repository.get.return_value = None
    result = await service.update_user(fake_db, "missing-id", UserUpdate(full_name="New"))
    assert result is None
    fake_db.commit.assert_not_awaited()


async def test_update_user_hashes_new_password(service, fake_db, sample_user):
    service.repository.get.return_value = sample_user
    service.repository.update.return_value = sample_user

    await service.update_user(fake_db, sample_user.id, UserUpdate(password="brand-new-pass"))

    update_data = service.repository.update.call_args.args[2]
    assert "password" not in update_data
    assert verify_password("brand-new-pass", update_data["password_hash"]) is True
    fake_db.commit.assert_awaited_once()


async def test_update_user_without_password_leaves_hash_untouched(service, fake_db, sample_user):
    service.repository.get.return_value = sample_user
    service.repository.update.return_value = sample_user

    await service.update_user(fake_db, sample_user.id, UserUpdate(full_name="Renamed"))

    update_data = service.repository.update.call_args.args[2]
    assert update_data == {"full_name": "Renamed"}
    assert "password_hash" not in update_data
