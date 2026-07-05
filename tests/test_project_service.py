import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.models.project import Project, ProjectStatus
from app.services import project_service as project_service_module
from app.services.project_service import ProjectService


class StubUpload:
    """Minimal stand-in for Starlette's UploadFile."""

    def __init__(self, data: bytes, content_type: str, filename: str = "song.wav") -> None:
        self._data = data
        self.content_type = content_type
        self.filename = filename

    async def read(self) -> bytes:
        return self._data


@pytest.fixture
def service() -> ProjectService:
    svc = ProjectService()
    svc.repository = AsyncMock()
    svc.file_repository = AsyncMock()
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


async def test_create_and_upload_success(service, fake_db, monkeypatch):
    user_id = uuid.uuid4()
    project = _project(user_id)
    service.repository.create.return_value = project
    service.repository.update.return_value = project

    upload_mock = MagicMock(return_value="key")
    monkeypatch.setattr(project_service_module.storage, "upload_file", upload_mock)

    result = await service.create_and_upload(
        fake_db, user_id=user_id, name="Song", file=StubUpload(b"audio-bytes", "audio/wav")
    )

    assert result is project
    # Object stored under a user/project-namespaced key.
    key = upload_mock.call_args.args[1]
    assert key == f"{user_id}/{project.id}/song.wav"
    service.file_repository.create_for_project.assert_awaited_once()
    service.repository.update.assert_awaited_once()
    assert service.repository.update.call_args.args[2] == {"status": ProjectStatus.QUEUED}
    fake_db.commit.assert_awaited_once()


async def test_rejects_unsupported_content_type(service, fake_db):
    with pytest.raises(ValueError, match="Unsupported audio content type"):
        await service.create_and_upload(
            fake_db,
            user_id=uuid.uuid4(),
            name="Song",
            file=StubUpload(b"data", "application/zip"),
        )
    service.repository.create.assert_not_called()


async def test_rejects_empty_file(service, fake_db):
    with pytest.raises(ValueError, match="empty"):
        await service.create_and_upload(
            fake_db, user_id=uuid.uuid4(), name="Song", file=StubUpload(b"", "audio/wav")
        )
    service.repository.create.assert_not_called()


async def test_rejects_file_over_limit(service, fake_db, monkeypatch):
    from app.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "max_upload_size_bytes", 4, raising=False)
    with pytest.raises(ValueError, match="maximum size"):
        await service.create_and_upload(
            fake_db,
            user_id=uuid.uuid4(),
            name="Song",
            file=StubUpload(b"too-many-bytes", "audio/wav"),
        )
    service.repository.create.assert_not_called()


async def test_create_upload_url_success(service, fake_db, monkeypatch):
    user_id = uuid.uuid4()
    project = _project(user_id)
    service.repository.create.return_value = project

    signed_mock = MagicMock(return_value={"url": "https://up.example", "token": "tok", "path": "p"})
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
    assert signed["token"] == "tok"
    # The signed URL is scoped to the user/project-namespaced key.
    assert signed_mock.call_args.args[1] == f"{user_id}/{project.id}/song.wav"
    fake_db.commit.assert_awaited_once()


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
    project.input_file = None
    service.repository.get_for_user.return_value = project
    service.repository.update.return_value = project

    list_mock = MagicMock(
        return_value=[{"name": "song.wav", "metadata": {"size": 2048, "mimetype": "audio/wav"}}]
    )
    monkeypatch.setattr(project_service_module.storage, "list_objects", list_mock)

    result = await service.complete_upload(fake_db, project_id=project.id, user_id=user_id)

    assert result is project
    service.file_repository.create_for_project.assert_awaited_once()
    kwargs = service.file_repository.create_for_project.call_args.kwargs
    assert kwargs["storage_key"] == f"{user_id}/{project.id}/song.wav"
    assert kwargs["file_size_bytes"] == 2048
    assert kwargs["content_type"] == "audio/wav"
    assert service.repository.update.call_args.args[2] == {"status": ProjectStatus.QUEUED}
    fake_db.commit.assert_awaited_once()


async def test_complete_upload_missing_project_returns_none(service, fake_db):
    service.repository.get_for_user.return_value = None
    result = await service.complete_upload(
        fake_db, project_id=uuid.uuid4(), user_id=uuid.uuid4()
    )
    assert result is None
    service.file_repository.create_for_project.assert_not_called()


async def test_complete_upload_no_object_raises(service, fake_db, monkeypatch):
    user_id = uuid.uuid4()
    project = _project(user_id)
    project.input_file = None
    service.repository.get_for_user.return_value = project

    monkeypatch.setattr(
        project_service_module.storage, "list_objects", MagicMock(return_value=[])
    )

    with pytest.raises(ValueError, match="No uploaded file found"):
        await service.complete_upload(fake_db, project_id=project.id, user_id=user_id)
    service.file_repository.create_for_project.assert_not_called()


async def test_cleans_up_storage_on_db_failure(service, fake_db, monkeypatch):
    user_id = uuid.uuid4()
    project = _project(user_id)
    service.repository.create.return_value = project
    service.file_repository.create_for_project.side_effect = RuntimeError("db down")

    upload_mock = MagicMock(return_value="key")
    delete_mock = MagicMock()
    monkeypatch.setattr(project_service_module.storage, "upload_file", upload_mock)
    monkeypatch.setattr(project_service_module.storage, "delete_file", delete_mock)

    with pytest.raises(RuntimeError, match="db down"):
        await service.create_and_upload(
            fake_db, user_id=user_id, name="Song", file=StubUpload(b"audio", "audio/wav")
        )

    fake_db.rollback.assert_awaited_once()
    delete_mock.assert_called_once()  # orphaned object removed
