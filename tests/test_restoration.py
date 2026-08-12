"""Restoration proxy tests.

Network-free like the rest of the suite: the upstream restoration service is a
stub ASGI app mounted on the httpx transport, so the real request/response and
error-translation paths run without a GPU host existing.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.core import storage
from app.core.config import get_settings
from app.models.project import Project, ProjectStatus
from app.models.project_file import ProjectFile
from app.models.restoration_job import RestorationJob, RestorationStage, RestorationStatus
from app.schemas.restoration import RestorationStartRequest
from app.services.restoration_service import RestorationError, restoration_service

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def enabled_settings():
    """Point the service at a stub upstream for the duration of a test."""
    settings = get_settings()
    url, key = settings.restoration_api_url, settings.restoration_api_key
    settings.restoration_api_url = "http://restore.test"
    settings.restoration_api_key = "test-key"
    yield settings
    settings.restoration_api_url, settings.restoration_api_key = url, key


def make_project(user_id: uuid.UUID, with_file: bool = True) -> Project:
    project = Project(
        id=uuid.uuid4(),
        user_id=user_id,
        name="Song",
        status=ProjectStatus.COMPLETED,
        created_at=NOW,
        updated_at=NOW,
    )
    project.input_files = []
    if with_file:
        project.input_files = [
            ProjectFile(
                id=uuid.uuid4(),
                project_id=project.id,
                original_filename="song.wav",
                storage_key=f"{user_id}/{project.id}/song.wav",
                file_size_bytes=1000,
                content_type="audio/wav",
                created_at=NOW,
                updated_at=NOW,
            )
        ]
    return project


def make_job(**overrides) -> RestorationJob:
    values = dict(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        stage=RestorationStage.V2,
        status=RestorationStatus.QUEUED,
        remote_job_id="abc123",
        source_key="u/p/song.wav",
        created_at=NOW,
        updated_at=NOW,
    )
    values.update(overrides)
    return RestorationJob(**values)


def patch_repo(name, value):
    """Shorthand for stubbing one repository method on the singleton service."""
    return patch.object(restoration_service.repository, name, AsyncMock(return_value=value))


def stub_upstream(handler):
    """Patch the service's client onto a MockTransport running `handler`."""
    def _client(self, settings):
        return httpx.AsyncClient(
            base_url=settings.restoration_base_url,
            headers={"X-API-Key": settings.restoration_api_key},
            transport=httpx.MockTransport(handler),
        )

    return patch.object(type(restoration_service), "_client", _client)


# --- disabled ------------------------------------------------------------


async def test_start_raises_when_not_configured(fake_db):
    settings = get_settings()
    url = settings.restoration_api_url
    settings.restoration_api_url = ""
    try:
        with pytest.raises(RestorationError) as exc:
            await restoration_service.start(
                fake_db,
                project_id=uuid.uuid4(),
                user_id=uuid.uuid4(),
                payload=RestorationStartRequest(),
            )
        assert exc.value.status_code == 503
    finally:
        settings.restoration_api_url = url


async def test_status_reports_disabled_without_config():
    settings = get_settings()
    url = settings.restoration_api_url
    settings.restoration_api_url = ""
    try:
        status = await restoration_service.service_status()
        assert status.enabled is False and status.reachable is False
    finally:
        settings.restoration_api_url = url


# --- starting a run ------------------------------------------------------


async def test_start_v2_uses_input_bucket_and_scoped_prefix(fake_db, enabled_settings):
    """The source is the *inputs* bucket; results are namespaced by user+project.

    Getting the bucket wrong is the failure this guards: the upstream reads from
    its own results bucket by default, where an original upload never lives.
    """
    user_id = uuid.uuid4()
    project = make_project(user_id)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = __import__("json").loads(request.content)
        seen["key"] = request.headers.get("x-api-key")
        return httpx.Response(202, json={"job_id": "j1", "status": "queued", "poll": "/jobs/j1"})

    with stub_upstream(handler), \
         patch("app.services.restoration_service.project_repository") as repo, \
         patch_repo("get_active", None), \
         patch.object(
             restoration_service.repository, "create",
             AsyncMock(side_effect=lambda db, **kw: make_job(**kw))):
        repo.get_for_user = AsyncMock(return_value=project)
        job = await restoration_service.start(
            fake_db, project_id=project.id, user_id=user_id, payload=RestorationStartRequest()
        )

    assert seen["path"] == "/v2/repair-from-s3"
    assert seen["key"] == "test-key"
    assert seen["body"]["source_bucket"] == storage.BUCKET_INPUTS
    assert seen["body"]["source_key"] == project.input_files[0].storage_key
    assert seen["body"]["key_prefix"] == f"{user_id}/{project.id}/restored"
    assert job.remote_job_id == "j1"


async def test_start_v3_after_v2_consumes_the_v2_output(fake_db, enabled_settings):
    """The chain: V3's source_key is the finished V2 job's output key."""
    user_id = uuid.uuid4()
    project = make_project(user_id)
    parent = make_job(
        project_id=project.id,
        user_id=user_id,
        stage=RestorationStage.V2,
        status=RestorationStatus.DONE,
        output_key="u/p/restored/v2/song_v2.wav",
        output_bucket="audio-stems",
    )
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = __import__("json").loads(request.content)
        return httpx.Response(202, json={"job_id": "j2", "status": "queued", "poll": "/jobs/j2"})

    with stub_upstream(handler), \
         patch("app.services.restoration_service.project_repository") as repo, \
         patch_repo("get_active", None), \
         patch_repo("get_for_user", parent), \
         patch.object(
             restoration_service.repository, "create",
             AsyncMock(side_effect=lambda db, **kw: make_job(**kw))):
        repo.get_for_user = AsyncMock(return_value=project)
        job = await restoration_service.start(
            fake_db,
            project_id=project.id,
            user_id=user_id,
            payload=RestorationStartRequest(
                stage=RestorationStage.V3, after_job_id=parent.id, bandwidth=False
            ),
        )

    assert seen["path"] == "/v3/restore-from-s3"
    assert seen["body"]["source_key"] == parent.output_key
    assert seen["body"]["source_bucket"] == "audio-stems"
    assert seen["body"]["bandwidth"] is False
    assert job.parent_id == parent.id


async def test_v3_refuses_an_unfinished_parent(fake_db, enabled_settings):
    """V3 cannot start early — its input is a file V2 has not written yet."""
    user_id = uuid.uuid4()
    project = make_project(user_id)
    parent = make_job(
        project_id=project.id, user_id=user_id, status=RestorationStatus.RUNNING, output_key=None
    )

    with patch("app.services.restoration_service.project_repository") as repo, \
         patch_repo("get_active", None), \
         patch_repo("get_for_user", parent):
        repo.get_for_user = AsyncMock(return_value=project)
        with pytest.raises(RestorationError) as exc:
            await restoration_service.start(
                fake_db,
                project_id=project.id,
                user_id=user_id,
                payload=RestorationStartRequest(
                    stage=RestorationStage.V3, after_job_id=parent.id
                ),
            )
    assert exc.value.status_code == 409


async def test_start_is_idempotent_while_a_stage_is_running(fake_db, enabled_settings):
    """One GPU, one job: a duplicate submit must not queue behind its own twin."""
    user_id = uuid.uuid4()
    project = make_project(user_id)
    running = make_job(project_id=project.id, user_id=user_id, status=RestorationStatus.RUNNING)
    called = MagicMock()

    def handler(request):
        called()
        return httpx.Response(202, json={"job_id": "nope", "status": "queued", "poll": "/x"})

    with stub_upstream(handler), \
         patch("app.services.restoration_service.project_repository") as repo, \
         patch_repo("get_active", running):
        repo.get_for_user = AsyncMock(return_value=project)
        job = await restoration_service.start(
            fake_db, project_id=project.id, user_id=user_id, payload=RestorationStartRequest()
        )

    assert job is running
    called.assert_not_called()


async def test_start_registers_an_upload_that_was_never_completed(fake_db, enabled_settings):
    """The browser uploads straight to S3, so the bytes exist before any DB row does.

    A restoration run does not call the upload-complete *endpoint* (that also
    enqueues a separation job, a different pipeline). Without this, every run
    failed with "Upload an audio file before starting restoration" even though the
    object was sitting in the bucket.
    """
    user_id = uuid.uuid4()
    empty = make_project(user_id, with_file=False)
    registered = make_project(user_id, with_file=True)
    registered.id = empty.id
    seen = {}

    def handler(request):
        seen["body"] = __import__("json").loads(request.content)
        return httpx.Response(202, json={"job_id": "j1", "status": "queued", "poll": "/jobs/j1"})

    with stub_upstream(handler), \
         patch("app.services.restoration_service.project_repository") as repo, \
         patch("app.services.restoration_service.project_service") as svc, \
         patch_repo("get_active", None), \
         patch.object(
             restoration_service.repository, "create",
             AsyncMock(side_effect=lambda db, **kw: make_job(**kw))):
        repo.get_for_user = AsyncMock(return_value=empty)
        svc.complete_upload = AsyncMock(return_value=registered)
        job = await restoration_service.start(
            fake_db, project_id=empty.id, user_id=user_id, payload=RestorationStartRequest()
        )

    svc.complete_upload.assert_awaited_once()
    assert seen["body"]["source_key"] == registered.input_files[0].storage_key
    assert job.remote_job_id == "j1"


async def test_start_requires_an_input_file(fake_db, enabled_settings):
    """Nothing recorded *and* nothing in the bucket — a genuinely empty project."""
    user_id = uuid.uuid4()
    project = make_project(user_id, with_file=False)
    with patch("app.services.restoration_service.project_repository") as repo, \
         patch("app.services.restoration_service.project_service") as svc, \
         patch_repo("get_active", None):
        repo.get_for_user = AsyncMock(return_value=project)
        # What complete_upload really raises when the prefix holds no objects.
        svc.complete_upload = AsyncMock(
            side_effect=ValueError("No uploaded file found for this project")
        )
        with pytest.raises(RestorationError) as exc:
            await restoration_service.start(
                fake_db, project_id=project.id, user_id=user_id, payload=RestorationStartRequest()
            )
    assert exc.value.status_code == 400


# --- polling -------------------------------------------------------------


async def test_sync_records_a_finished_run(fake_db, enabled_settings):
    job = make_job(status=RestorationStatus.RUNNING)

    def handler(request):
        return httpx.Response(200, json={
            "job_id": "abc123", "kind": "v2", "status": "done", "filename": "song.wav",
            "progress": ["Separating stems", "Done"],
            "outputs": {"v2": {"key": "u/p/restored/v2/song_v2.wav", "url": "https://x/y"}},
            "bucket": "audio-stems",
            "report": {"outcome": "enhanced"}, "quality": None, "error": None,
            "created_at": NOW.isoformat(), "finished_at": NOW.isoformat(),
        })

    with stub_upstream(handler):
        result = await restoration_service.sync(fake_db, job)

    assert result.status is RestorationStatus.DONE
    assert result.output_key == "u/p/restored/v2/song_v2.wav"
    assert result.output_bucket == "audio-stems"
    assert result.report == {"outcome": "enhanced"}
    assert result.completed_at is not None


async def test_sync_does_not_refetch_a_finished_job(fake_db, enabled_settings):
    """The upstream evicts jobs; re-asking risks trading a stored result for a 404."""
    job = make_job(status=RestorationStatus.DONE, output_key="k")
    called = MagicMock()

    def handler(request):
        called()
        return httpx.Response(404, json={"detail": "Unknown job."})

    with stub_upstream(handler):
        result = await restoration_service.sync(fake_db, job)

    assert result.status is RestorationStatus.DONE
    called.assert_not_called()


async def test_sync_fails_a_job_the_upstream_forgot(fake_db, enabled_settings):
    """A restarted upstream must end the run, not leave the UI spinning forever."""
    job = make_job(status=RestorationStatus.RUNNING)

    def handler(request):
        return httpx.Response(404, json={"detail": "Unknown job. Jobs do not survive a restart."})

    with stub_upstream(handler):
        result = await restoration_service.sync(fake_db, job)

    assert result.status is RestorationStatus.FAILED
    assert "restarted" in (result.error_message or "")


async def test_upstream_401_is_not_reported_as_a_user_auth_problem(fake_db, enabled_settings):
    """A bad server-side key is a 502 misconfiguration, not the caller's 401."""
    job = make_job(status=RestorationStatus.RUNNING)

    def handler(request):
        return httpx.Response(401, json={"detail": "Invalid or missing X-API-Key"})

    with stub_upstream(handler):
        with pytest.raises(RestorationError) as exc:
            await restoration_service.sync(fake_db, job)

    assert exc.value.status_code == 502
    assert "X-API-Key" not in str(exc.value)


async def test_busy_queue_surfaces_as_503(fake_db, enabled_settings):
    user_id = uuid.uuid4()
    project = make_project(user_id)

    def handler(request):
        return httpx.Response(503, json={"detail": "16 jobs already waiting"})

    with stub_upstream(handler), \
         patch("app.services.restoration_service.project_repository") as repo, \
         patch_repo("get_active", None):
        repo.get_for_user = AsyncMock(return_value=project)
        with pytest.raises(RestorationError) as exc:
            await restoration_service.start(
                fake_db, project_id=project.id, user_id=user_id, payload=RestorationStartRequest()
            )

    assert exc.value.status_code == 503
    assert "waiting" in str(exc.value)


async def test_html_response_reads_as_unreachable_not_a_crash(enabled_settings):
    """A URL pointing at the wrong service must degrade, not 500.

    Streamlit answers every path with its HTML shell — including /health — so a URL
    left pointing at the demo UI returns 200 with HTML. Parsing that as JSON used to
    raise straight out of the status endpoint whose whole job is to report the
    problem.
    """
    def handler(request):
        return httpx.Response(200, html="<!DOCTYPE html><html><body>Streamlit</body></html>")

    with stub_upstream(handler):
        status = await restoration_service.service_status()

    assert status.enabled is True
    assert status.reachable is False
    assert "RESTORATION_API_URL" in (status.detail or "")


# --- presentation --------------------------------------------------------


def test_to_response_signs_a_fresh_url(enabled_settings):
    """URLs are minted per request; a stored one would be dead on page reload."""
    job = make_job(
        status=RestorationStatus.DONE,
        output_key="u/p/restored/v3/song_restored.wav",
        output_bucket="audio-stems",
        progress=["Step 1", "Step 2 - denoise"],
    )
    with patch("app.services.restoration_service.storage.get_signed_url", return_value="https://signed"):
        body = restoration_service.to_response(job)

    assert body.output is not None
    assert body.output.url == "https://signed"
    assert body.current_step == "Step 2 - denoise"
    assert body.is_finished is True


def test_to_response_has_no_output_before_completion(enabled_settings):
    body = restoration_service.to_response(make_job(status=RestorationStatus.RUNNING))
    assert body.output is None
    assert body.is_finished is False
    assert body.current_step == ""
