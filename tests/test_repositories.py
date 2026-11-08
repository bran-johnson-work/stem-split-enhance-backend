import uuid

import pytest
from pydantic import BaseModel

from app.models.user import User
from app.repositories.base_repository import BaseRepository
from app.repositories.user_repository import UserRepository


class _UserInsert(BaseModel):
    """A create-schema whose fields all map to real User columns."""

    email: str
    full_name: str | None = None


@pytest.fixture
def repo() -> BaseRepository:
    return BaseRepository(User)


@pytest.fixture
def user_repo() -> UserRepository:
    return UserRepository(User)


# --- BaseRepository -------------------------------------------------------


async def test_get_returns_first_scalar(repo, fake_db, execute_result, sample_user):
    fake_db.execute.return_value = execute_result(sample_user)
    got = await repo.get(fake_db, sample_user.id)
    assert got is sample_user
    fake_db.execute.assert_awaited_once()


async def test_get_multi_returns_all_scalars(repo, fake_db, execute_result, sample_user):
    fake_db.execute.return_value = execute_result([sample_user])
    got = await repo.get_multi(fake_db, skip=0, limit=10)
    assert got == [sample_user]


async def test_create_builds_model_and_persists(repo, fake_db):
    created = await repo.create(fake_db, _UserInsert(email="new@example.com", full_name="New"))
    assert isinstance(created, User)
    assert created.email == "new@example.com"
    assert created.full_name == "New"
    fake_db.add.assert_called_once()
    fake_db.flush.assert_awaited_once()
    fake_db.refresh.assert_awaited_once()


async def test_update_with_dict_sets_fields(repo, fake_db, sample_user):
    updated = await repo.update(fake_db, sample_user, {"full_name": "Changed"})
    assert updated is sample_user
    assert sample_user.full_name == "Changed"
    fake_db.flush.assert_awaited_once()


async def test_update_with_schema_only_sets_provided_fields(repo, fake_db, sample_user):
    from app.schemas.user import UserUpdate

    original_email = sample_user.email
    await repo.update(fake_db, sample_user, UserUpdate(full_name="Only Name"))
    assert sample_user.full_name == "Only Name"
    assert sample_user.email == original_email  # unset fields untouched


async def test_delete_returns_true_when_found(repo, fake_db, execute_result, sample_user):
    fake_db.execute.return_value = execute_result(sample_user)
    assert await repo.delete(fake_db, sample_user.id) is True
    fake_db.delete.assert_awaited_once_with(sample_user)


async def test_delete_returns_false_when_missing(repo, fake_db, execute_result):
    fake_db.execute.return_value = execute_result(None)
    assert await repo.delete(fake_db, uuid.uuid4()) is False
    fake_db.delete.assert_not_awaited()


# --- UserRepository -------------------------------------------------------


async def test_get_by_email(user_repo, fake_db, execute_result, sample_user):
    fake_db.execute.return_value = execute_result(sample_user)
    assert await user_repo.get_by_email(fake_db, "user@example.com") is sample_user


async def test_create_user_with_hash_sets_defaults(user_repo, fake_db):
    from app.schemas.user import UserCreate

    user_in = UserCreate(email="x@y.com", password="ignored-here", full_name="X")
    created = await user_repo.create_user_with_hash(fake_db, user_in, "salt:digest")

    assert isinstance(created, User)
    assert created.email == "x@y.com"
    assert created.password_hash == "salt:digest"
    assert created.full_name == "X"
    assert created.is_active is True
    assert created.is_verified is False
    fake_db.add.assert_called_once()
    fake_db.flush.assert_awaited_once()


async def test_is_active_true(user_repo, fake_db, execute_result, sample_user):
    sample_user.is_active = True
    fake_db.execute.return_value = execute_result(sample_user)
    assert await user_repo.is_active(fake_db, sample_user.id) is True


async def test_is_active_false_when_user_missing(user_repo, fake_db, execute_result):
    fake_db.execute.return_value = execute_result(None)
    assert await user_repo.is_active(fake_db, uuid.uuid4()) is False
