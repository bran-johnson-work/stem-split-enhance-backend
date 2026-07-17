import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.project import Project, ProjectStatus
from app.models.subscription import SubscriptionStatus
from app.services import project_service as project_service_module
from app.services.project_service import ProjectService


@pytest.fixture
def service() -> ProjectService:
    svc = ProjectService()
    svc.repository = AsyncMock()
    svc.file_repository = AsyncMock()
    # Quota + file-size enforcement are exercised in dedicated tests; neutralise
    # them elsewhere (no plan => the file-size gate is a no-op).
    svc._enforce_project_quota = AsyncMock()
    svc._effective_plan = AsyncMock(return_value=None)
    return svc


def _project(user_id: uuid.UUID) -> Project:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Project(
        id=uuid.uuid4(),
        user_id=user_id,
        name="Song",
        status=ProjectStatus.UPLOADING,
        created_at=now,
        updated_at=now,
    )


async def test_create_upload_url_success(service, fake_db, monkeypatch):
    user_id = uuid.uuid4()
    project = _project(user_id)
    service.repository.create.return_value = project

    signed_mock = MagicMock(
        return_value={"url": "https://up.example", "fields": {"key": "k"}, "path": "p"}
    )
    monkeypatch.setattr(
        project_service_module.storage, "create_signed_upload_url", signed_mock
    )

    result_project, signed = await service.create_upload_url(
        fake_db,
        user_id=user_id,
        name="Song",
        filename="song.wav",
        content_type="audio/wav",
    )

    assert result_project is project
    assert signed["storage_key"] == f"{user_id}/{project.id}/song.wav"
    assert signed["url"] == "https://up.example"
    assert signed["fields"] == {"key": "k"}
    # The signed POST is scoped to the user/project-namespaced key.
    assert signed_mock.call_args.args[1] == f"{user_id}/{project.id}/song.wav"
    fake_db.commit.assert_awaited_once()


@pytest.mark.parametrize(
    "plan_mb",
    [
        20,  # free: below the MAX_UPLOAD_SIZE_BYTES fallback (50 MB)
        100,  # creator: above it — must NOT be clamped down to the fallback
        500,  # studio
    ],
)
async def test_presign_policy_carries_the_users_own_plan_limit(
    service, fake_db, monkeypatch, plan_mb
):
    """S3 has no bucket-level limit, so the presign must carry the plan's own cap.

    The plan is authoritative in both directions: MAX_UPLOAD_SIZE_BYTES is a
    fallback, not a ceiling, so a paid tier above it is passed through intact.
    """
    user_id = uuid.uuid4()
    service.repository.create.return_value = _project(user_id)
    service._effective_plan = AsyncMock(return_value=SimpleNamespace(max_file_size_mb=plan_mb))

    signed_mock = MagicMock(return_value={"url": "u", "fields": {}, "path": "p"})
    monkeypatch.setattr(
        project_service_module.storage, "create_signed_upload_url", signed_mock
    )

    await service.create_upload_url(
        fake_db,
        user_id=user_id,
        name="Song",
        filename="song.wav",
        content_type="audio/wav",
    )

    kwargs = signed_mock.call_args.kwargs
    assert kwargs["max_size_bytes"] == plan_mb * 1024 * 1024
    assert kwargs["content_type"] == "audio/wav"


async def test_presign_policy_falls_back_to_settings_when_plan_unresolvable(
    service, fake_db, monkeypatch
):
    """No plan row (free unseeded) still needs a concrete number for the policy."""
    user_id = uuid.uuid4()
    service.repository.create.return_value = _project(user_id)
    service._effective_plan = AsyncMock(return_value=None)

    signed_mock = MagicMock(return_value={"url": "u", "fields": {}, "path": "p"})
    monkeypatch.setattr(
        project_service_module.storage, "create_signed_upload_url", signed_mock
    )

    await service.create_upload_url(
        fake_db,
        user_id=user_id,
        name="Song",
        filename="song.wav",
        content_type="audio/wav",
    )

    expected = project_service_module.get_settings().max_upload_size_bytes
    assert signed_mock.call_args.kwargs["max_size_bytes"] == expected


async def test_create_upload_url_rejects_bad_type(service, fake_db):
    with pytest.raises(ValueError, match="Unsupported audio content type"):
        await service.create_upload_url(
            fake_db,
            user_id=uuid.uuid4(),
            name="Song",
            filename="x.zip",
            content_type="application/zip",
        )
    service.repository.create.assert_not_called()


async def test_complete_upload_records_stored_file(service, fake_db, monkeypatch):
    user_id = uuid.uuid4()
    project = _project(user_id)
    project.input_files = []
    service.repository.get_for_user.return_value = project
    service.repository.update.return_value = project

    list_mock = MagicMock(
        return_value=[{"name": "song.wav", "metadata": {"size": 2048, "mimetype": "audio/wav"}}]
    )
    monkeypatch.setattr(project_service_module.storage, "list_objects", list_mock)

    result = await service.complete_upload(fake_db, project_id=project.id, user_id=user_id)

    assert result is project
    service.file_repository.upsert_for_project.assert_awaited_once()
    kwargs = service.file_repository.upsert_for_project.call_args.kwargs
    assert kwargs["storage_key"] == f"{user_id}/{project.id}/song.wav"
    assert kwargs["file_size_bytes"] == 2048
    assert kwargs["content_type"] == "audio/wav"
    assert service.repository.update.call_args.args[2] == {"status": ProjectStatus.QUEUED}
    fake_db.commit.assert_awaited_once()


async def test_complete_upload_rejects_oversize_file(service, fake_db, monkeypatch):
    """A stored file over the plan limit is purged and the call fails with 413-path."""
    from app.services.project_service import FileTooLargeError

    user_id = uuid.uuid4()
    project = _project(user_id)
    project.input_files = []
    service.repository.get_for_user.return_value = project
    # Effective plan caps uploads at 20 MB; the stored object is 21 MB.
    service._effective_plan = AsyncMock(return_value=SimpleNamespace(max_file_size_mb=20))

    twenty_one_mb = 21 * 1024 * 1024
    list_mock = MagicMock(
        return_value=[{"name": "big.wav", "metadata": {"size": twenty_one_mb}}]
    )
    delete_mock = MagicMock()
    monkeypatch.setattr(project_service_module.storage, "list_objects", list_mock)
    monkeypatch.setattr(project_service_module.storage, "delete_file", delete_mock)

    with pytest.raises(FileTooLargeError, match="20 MB"):
        await service.complete_upload(fake_db, project_id=project.id, user_id=user_id)

    # The oversize object was removed and nothing was recorded/queued.
    delete_mock.assert_called_once()
    assert delete_mock.call_args.args[1] == f"{user_id}/{project.id}/big.wav"
    service.file_repository.upsert_for_project.assert_not_called()


async def test_complete_upload_allows_file_at_plan_limit(service, fake_db, monkeypatch):
    """A file exactly at the plan limit is accepted (boundary is inclusive)."""
    user_id = uuid.uuid4()
    project = _project(user_id)
    project.input_files = []
    service.repository.get_for_user.return_value = project
    service.repository.update.return_value = project
    service._effective_plan = AsyncMock(return_value=SimpleNamespace(max_file_size_mb=20))

    exactly_twenty_mb = 20 * 1024 * 1024
    monkeypatch.setattr(
        project_service_module.storage,
        "list_objects",
        MagicMock(return_value=[{"name": "ok.wav", "metadata": {"size": exactly_twenty_mb}}]),
    )

    result = await service.complete_upload(fake_db, project_id=project.id, user_id=user_id)

    assert result is project
    service.file_repository.upsert_for_project.assert_awaited_once()


async def test_create_upload_url_declared_size_over_limit_rejected(service, fake_db):
    """Presign fast-fail: a declared size over the plan limit rejects before upload."""
    from app.services.project_service import FileTooLargeError

    service._effective_plan = AsyncMock(return_value=SimpleNamespace(max_file_size_mb=20))

    with pytest.raises(FileTooLargeError, match="20 MB"):
        await service.create_upload_url(
            fake_db,
            user_id=uuid.uuid4(),
            name="Song",
            filename="song.wav",
            content_type="audio/wav",
            declared_size_bytes=21 * 1024 * 1024,
        )
    # Rejected before any project row or presigned URL is created.
    service.repository.create.assert_not_called()
    service._enforce_project_quota.assert_not_called()


async def test_complete_upload_missing_project_returns_none(service, fake_db):
    service.repository.get_for_user.return_value = None
    result = await service.complete_upload(
        fake_db, project_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    assert result is None
    service.file_repository.upsert_for_project.assert_not_called()


async def test_complete_upload_no_object_raises(service, fake_db, monkeypatch):
    user_id = uuid.uuid4()
    project = _project(user_id)
    project.input_files = []
    service.repository.get_for_user.return_value = project

    monkeypatch.setattr(
        project_service_module.storage, "list_objects", MagicMock(return_value=[])
    )

    with pytest.raises(ValueError, match="No uploaded file found"):
        await service.complete_upload(fake_db, project_id=project.id, user_id=user_id)
    service.file_repository.upsert_for_project.assert_not_called()


# --- create empty project --------------------------------------------------


async def test_create_project_enforces_quota_then_creates(service, fake_db):
    user_id = uuid.uuid4()
    project = _project(user_id)
    service.repository.create.return_value = project

    result = await service.create_project(fake_db, user_id=user_id, name="Song")

    assert result is project
    service._enforce_project_quota.assert_awaited_once()
    # Created as an empty PENDING project (no file yet).
    created = service.repository.create.call_args.args[1]
    assert created.status is ProjectStatus.PENDING
    fake_db.commit.assert_awaited_once()


async def test_create_project_blocked_by_quota_does_not_create(service, fake_db):
    service._enforce_project_quota.side_effect = ValueError("Monthly project limit reached")
    with pytest.raises(ValueError, match="limit reached"):
        await service.create_project(fake_db, user_id=uuid.uuid4(), name="Song")
    service.repository.create.assert_not_called()


async def test_create_file_upload_url_delegates_with_project(service, fake_db, monkeypatch):
    user_id = uuid.uuid4()
    project = _project(user_id)
    service.repository.get_for_user.return_value = project
    service.repository.update.return_value = project

    signed_mock = MagicMock(
        return_value={"url": "https://up.example", "fields": {"key": "k"}, "path": "p"}
    )
    monkeypatch.setattr(project_service_module.storage, "create_signed_upload_url", signed_mock)

    result = await service.create_file_upload_url(
        fake_db,
        project_id=project.id,
        user_id=user_id,
        filename="song.wav",
        content_type="audio/wav",
    )

    assert result is not None
    result_project, signed = result
    assert result_project is project
    assert signed["storage_key"] == f"{user_id}/{project.id}/song.wav"
    # Existing project → no quota consumed, no new project created.
    service._enforce_project_quota.assert_not_called()
    service.repository.create.assert_not_called()


async def test_create_file_upload_url_missing_project_returns_none(service, fake_db):
    service.repository.get_for_user.return_value = None
    result = await service.create_file_upload_url(
        fake_db,
        project_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        filename="song.wav",
        content_type="audio/wav",
    )
    assert result is None


# --- deletion --------------------------------------------------------------


async def test_delete_input_file_removes_storage_and_row(service, fake_db, monkeypatch):
    user_id = uuid.uuid4()
    project = _project(user_id)
    service.repository.get_for_user.return_value = project
    target = SimpleNamespace(id=uuid.uuid4(), storage_key=f"{user_id}/{project.id}/song.wav")
    service.file_repository.get_for_project.return_value = target

    delete_mock = MagicMock()
    monkeypatch.setattr(project_service_module.storage, "delete_file", delete_mock)

    result = await service.delete_input_file(
        fake_db, project_id=project.id, user_id=user_id, file_id=target.id
    )

    assert result is True
    delete_mock.assert_called_once()
    assert delete_mock.call_args.args[1] == target.storage_key
    fake_db.delete.assert_awaited_once_with(target)
    fake_db.commit.assert_awaited_once()


async def test_delete_input_file_missing_project_returns_none(service, fake_db):
    service.repository.get_for_user.return_value = None
    result = await service.delete_input_file(
        fake_db, project_id=uuid.uuid4(), user_id=uuid.uuid4(), file_id=uuid.uuid4()
    )
    assert result is None
    service.file_repository.get_for_project.assert_not_called()


async def test_delete_input_file_missing_file_returns_false(service, fake_db):
    project = _project(uuid.uuid4())
    service.repository.get_for_user.return_value = project
    service.file_repository.get_for_project.return_value = None
    result = await service.delete_input_file(
        fake_db, project_id=project.id, user_id=project.user_id, file_id=uuid.uuid4()
    )
    assert result is False
    fake_db.delete.assert_not_called()


async def test_delete_project_purges_storage_and_row(service, fake_db, monkeypatch):
    user_id = uuid.uuid4()
    project = _project(user_id)
    service.repository.get_for_user.return_value = project

    prefix_mock = MagicMock()
    monkeypatch.setattr(project_service_module.storage, "delete_prefix", prefix_mock)

    result = await service.delete_project(fake_db, project_id=project.id, user_id=user_id)

    assert result is True
    # Purged both the input and stem buckets under the project prefix.
    assert prefix_mock.call_count == 2
    assert all(call.args[1] == f"{user_id}/{project.id}" for call in prefix_mock.call_args_list)
    fake_db.delete.assert_awaited_once_with(project)
    fake_db.commit.assert_awaited_once()


async def test_delete_project_missing_returns_none(service, fake_db):
    service.repository.get_for_user.return_value = None
    result = await service.delete_project(
        fake_db, project_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    assert result is None
    fake_db.delete.assert_not_called()


# --- monthly quota enforcement ---------------------------------------------


def _quota_service(monkeypatch, *, subscription, plan, used):
    """A service with the real quota method but mocked plan/subscription repos."""
    svc = ProjectService()
    svc.repository = AsyncMock()
    svc.repository.count_for_user_since.return_value = used

    sub_repo = AsyncMock()
    sub_repo.get_for_user.return_value = subscription
    plan_repo = AsyncMock()
    plan_repo.get_by_name.return_value = plan
    monkeypatch.setattr(project_service_module, "subscription_repository", sub_repo)
    monkeypatch.setattr(project_service_module, "plan_repository", plan_repo)
    return svc


async def test_quota_blocks_when_at_free_limit(fake_db, monkeypatch):
    # No subscription → falls back to the seeded free plan (3/month).
    svc = _quota_service(
        monkeypatch,
        subscription=None,
        plan=SimpleNamespace(max_projects_per_month=3),
        used=3,
    )
    with pytest.raises(ValueError, match="Monthly project limit reached"):
        await svc._enforce_project_quota(fake_db, uuid.uuid4())


async def test_quota_allows_under_limit(fake_db, monkeypatch):
    svc = _quota_service(
        monkeypatch,
        subscription=None,
        plan=SimpleNamespace(max_projects_per_month=3),
        used=2,
    )
    await svc._enforce_project_quota(fake_db, uuid.uuid4())  # no raise


async def test_quota_unlimited_plan_skips_count(fake_db, monkeypatch):
    now = datetime.now(timezone.utc)
    subscription = SimpleNamespace(
        status=SubscriptionStatus.ACTIVE,
        current_period_start=now,
        plan=SimpleNamespace(max_projects_per_month=None),
    )
    svc = _quota_service(monkeypatch, subscription=subscription, plan=None, used=999)
    await svc._enforce_project_quota(fake_db, uuid.uuid4())  # no raise
    svc.repository.count_for_user_since.assert_not_called()


async def test_quota_uses_active_subscription_window(fake_db, monkeypatch):
    now = datetime.now(timezone.utc)
    subscription = SimpleNamespace(
        status=SubscriptionStatus.ACTIVE,
        current_period_start=now,
        plan=SimpleNamespace(max_projects_per_month=20),
    )
    svc = _quota_service(monkeypatch, subscription=subscription, plan=None, used=20)
    with pytest.raises(ValueError, match="reached \\(20\\)"):
        await svc._enforce_project_quota(fake_db, uuid.uuid4())
    # Counted against the billing-period start, not the calendar month.
    assert svc.repository.count_for_user_since.call_args.args[2] == now
