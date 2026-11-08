import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.security import hash_password, verify_password
from app.schemas.user import UserCreate, UserUpdate
from app.services import user_service as user_service_module
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


# --- Avatars ----------------------------------------------------------------


async def test_avatar_url_for_none_when_no_key(service, sample_user):
    sample_user.avatar_key = None
    assert await service.avatar_url_for(sample_user) is None


async def test_avatar_url_for_signs_when_key_present(service, sample_user, monkeypatch):
    sample_user.avatar_key = f"avatar/{sample_user.id}/avatar"
    signer = MagicMock(return_value="https://signed.example/avatar")
    monkeypatch.setattr(user_service_module.storage, "get_signed_url", signer)

    url = await service.avatar_url_for(sample_user)

    assert url == "https://signed.example/avatar"
    # Signed against the avatars bucket + stored key.
    assert signer.call_args.args[0] == user_service_module.storage.BUCKET_AVATARS
    assert signer.call_args.args[1] == sample_user.avatar_key


async def test_serialize_injects_signed_avatar_url(service, sample_user, monkeypatch):
    sample_user.avatar_key = f"avatar/{sample_user.id}/avatar"
    monkeypatch.setattr(
        user_service_module.storage,
        "get_signed_url",
        MagicMock(return_value="https://signed.example/a"),
    )
    resp = await service.serialize(sample_user)
    assert resp.avatar_url == "https://signed.example/a"
    assert resp.email == sample_user.email


async def test_create_avatar_upload_url_success(service, sample_user, monkeypatch):
    signed = MagicMock(return_value={"url": "https://up", "fields": {"key": "k"}, "path": "p"})
    monkeypatch.setattr(user_service_module.storage, "create_signed_upload_url", signed)

    result = await service.create_avatar_upload_url(user=sample_user, content_type="image/png")

    assert result["storage_key"] == f"avatar/{sample_user.id}/avatar"
    assert result["url"] == "https://up"
    # Presign targets the avatars bucket at the deterministic per-user key.
    assert signed.call_args.args[0] == user_service_module.storage.BUCKET_AVATARS
    assert signed.call_args.args[1] == f"avatar/{sample_user.id}/avatar"
    assert signed.call_args.kwargs["content_type"] == "image/png"


async def test_create_avatar_upload_url_rejects_non_image(service, sample_user, monkeypatch):
    signed = MagicMock()
    monkeypatch.setattr(user_service_module.storage, "create_signed_upload_url", signed)

    with pytest.raises(ValueError, match="Unsupported image content type"):
        await service.create_avatar_upload_url(user=sample_user, content_type="application/zip")
    signed.assert_not_called()


async def test_complete_avatar_upload_records_key(service, fake_db, sample_user, monkeypatch):
    sample_user.avatar_key = None
    service.repository.update.return_value = sample_user
    monkeypatch.setattr(
        user_service_module.storage,
        "list_objects",
        MagicMock(return_value=[{"name": "avatar", "metadata": {"size": 1024}}]),
    )

    await service.complete_avatar_upload(fake_db, user=sample_user)

    assert service.repository.update.call_args.args[2] == {
        "avatar_key": f"avatar/{sample_user.id}/avatar"
    }
    fake_db.commit.assert_awaited_once()


async def test_complete_avatar_upload_missing_object_raises(
    service, fake_db, sample_user, monkeypatch
):
    monkeypatch.setattr(
        user_service_module.storage, "list_objects", MagicMock(return_value=[])
    )
    with pytest.raises(ValueError, match="No uploaded avatar found"):
        await service.complete_avatar_upload(fake_db, user=sample_user)
    service.repository.update.assert_not_called()


async def test_remove_avatar_deletes_object_and_clears_key(
    service, fake_db, sample_user, monkeypatch
):
    sample_user.avatar_key = f"avatar/{sample_user.id}/avatar"
    service.repository.update.return_value = sample_user
    delete_mock = MagicMock()
    monkeypatch.setattr(user_service_module.storage, "delete_file", delete_mock)

    await service.remove_avatar(fake_db, user=sample_user)

    delete_mock.assert_called_once()
    assert delete_mock.call_args.args[1] == f"avatar/{sample_user.id}/avatar"
    assert service.repository.update.call_args.args[2] == {"avatar_key": None}
    fake_db.commit.assert_awaited_once()
