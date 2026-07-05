import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.api.dependencies import get_current_user
from app.api.v1.endpoints import projects as projects_module
from app.main import app
from app.models.project import Project, ProjectStatus


@pytest.fixture
def auth_client(client, sample_user):
    app.dependency_overrides[get_current_user] = lambda: sample_user
    yield client
    app.dependency_overrides.pop(get_current_user, None)


def _project(user_id: uuid.UUID) -> Project:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    return Project(
        id=uuid.uuid4(),
        user_id=user_id,
        name="My song",
        status=ProjectStatus.QUEUED,
        created_at=now,
        updated_at=now,
    )


async def test_upload_audio_success(auth_client, monkeypatch, sample_user):
    project = _project(sample_user.id)
    monkeypatch.setattr(
        projects_module.project_service, "create_and_upload", AsyncMock(return_value=project)
    )
    resp = await auth_client.post(
        "/api/v1/projects/upload",
        files={"file": ("song.wav", b"audio-bytes", "audio/wav")},
        data={"name": "My song"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["id"] == str(project.id)
    assert body["status"] == "queued"


async def test_upload_audio_rejects_bad_type(auth_client, monkeypatch):
    monkeypatch.setattr(
        projects_module.project_service,
        "create_and_upload",
        AsyncMock(side_effect=ValueError("Unsupported audio content type: application/zip")),
    )
    resp = await auth_client.post(
        "/api/v1/projects/upload",
        files={"file": ("x.zip", b"data", "application/zip")},
    )
    assert resp.status_code == 400
    assert "Unsupported" in resp.json()["detail"]


async def test_create_upload_url_success(auth_client, monkeypatch, sample_user):
    project = _project(sample_user.id)
    signed = {"storage_key": "key/song.wav", "url": "https://up.example", "token": "tok"}
    monkeypatch.setattr(
        projects_module.project_service,
        "create_upload_url",
        AsyncMock(return_value=(project, signed)),
    )
    resp = await auth_client.post(
        "/api/v1/projects/upload-url",
        json={"filename": "song.wav", "content_type": "audio/wav", "name": "My song"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["project_id"] == str(project.id)
    assert body["url"] == "https://up.example"
    assert body["token"] == "tok"


async def test_create_upload_url_rejects_bad_type(auth_client, monkeypatch):
    monkeypatch.setattr(
        projects_module.project_service,
        "create_upload_url",
        AsyncMock(side_effect=ValueError("Unsupported audio content type: application/zip")),
    )
    resp = await auth_client.post(
        "/api/v1/projects/upload-url",
        json={"filename": "x.zip", "content_type": "application/zip"},
    )
    assert resp.status_code == 400
    assert "Unsupported" in resp.json()["detail"]


async def test_complete_upload_success(auth_client, monkeypatch, sample_user):
    project = _project(sample_user.id)
    monkeypatch.setattr(
        projects_module.project_service, "complete_upload", AsyncMock(return_value=project)
    )
    resp = await auth_client.post(f"/api/v1/projects/{project.id}/upload-complete")
    assert resp.status_code == 200
    assert resp.json()["status"] == "queued"


async def test_complete_upload_not_found(auth_client, monkeypatch):
    monkeypatch.setattr(
        projects_module.project_service, "complete_upload", AsyncMock(return_value=None)
    )
    resp = await auth_client.post(f"/api/v1/projects/{uuid.uuid4()}/upload-complete")
    assert resp.status_code == 404


async def test_complete_upload_no_file(auth_client, monkeypatch):
    monkeypatch.setattr(
        projects_module.project_service,
        "complete_upload",
        AsyncMock(side_effect=ValueError("No uploaded file found for this project")),
    )
    resp = await auth_client.post(f"/api/v1/projects/{uuid.uuid4()}/upload-complete")
    assert resp.status_code == 400
    assert "No uploaded file" in resp.json()["detail"]


async def test_upload_url_requires_auth(client):
    resp = await client.post(
        "/api/v1/projects/upload-url",
        json={"filename": "song.wav", "content_type": "audio/wav"},
    )
    assert resp.status_code == 401


async def test_list_projects(auth_client, monkeypatch, sample_user):
    project = _project(sample_user.id)
    monkeypatch.setattr(
        projects_module.project_service.repository,
        "list_for_user",
        AsyncMock(return_value=[project]),
    )
    resp = await auth_client.get("/api/v1/projects")
    assert resp.status_code == 200
    assert resp.json()[0]["id"] == str(project.id)


async def test_read_project_not_found(auth_client, monkeypatch):
    monkeypatch.setattr(
        projects_module.project_service.repository, "get_for_user", AsyncMock(return_value=None)
    )
    resp = await auth_client.get(f"/api/v1/projects/{uuid.uuid4()}")
    assert resp.status_code == 404


async def test_input_url_found(auth_client, monkeypatch):
    monkeypatch.setattr(
        projects_module.project_service,
        "get_input_url",
        AsyncMock(return_value="https://signed.example/audio"),
    )
    resp = await auth_client.get(f"/api/v1/projects/{uuid.uuid4()}/input-url")
    assert resp.status_code == 200
    assert resp.json()["url"] == "https://signed.example/audio"


async def test_input_url_not_found(auth_client, monkeypatch):
    monkeypatch.setattr(
        projects_module.project_service, "get_input_url", AsyncMock(return_value=None)
    )
    resp = await auth_client.get(f"/api/v1/projects/{uuid.uuid4()}/input-url")
    assert resp.status_code == 404


async def test_upload_requires_auth(client):
    resp = await client.post(
        "/api/v1/projects/upload",
        files={"file": ("song.wav", b"audio", "audio/wav")},
    )
    assert resp.status_code == 401
