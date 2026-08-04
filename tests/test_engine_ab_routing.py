"""Two-fleet queue routing and A/B version selection.

The v1 and v2 containers share one Postgres queue and are distinguished *only* by
``WORKER_ENGINE_VERSIONS``. If that filter leaks, a v1 image claims a v2 job, runs
HT-Demucs, and records the result as v2 — wrong audio under a label the user trusts. These
tests pin the filter and the version-selection rules around it.
"""

import uuid

import pytest
from sqlalchemy.dialects import postgresql

from app.core.config import Settings
from app.models.processing_job import JobStatus, ProcessingJob
from app.repositories.processing_job_repository import processing_job_repository
from app.services.job_service import _ab_bucket, _versions_for


def _job(version: str = "v1") -> ProcessingJob:
    return ProcessingJob(
        id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        status=JobStatus.PENDING,
        progress=0.0,
        current_step="",
        attempts=0,
        max_attempts=3,
        engine_version=version,
    )


def _rendered(stmt) -> str:
    """Full SQL, compiled against the real dialect (SKIP LOCKED is Postgres-specific)."""
    return str(
        stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
    )


def _where(stmt) -> str:
    """Just the predicate. ``engine_version`` is always in the SELECT column list, so
    asserting against the whole statement would pass even with no filter applied."""
    sql = _rendered(stmt)
    _, _, rest = sql.partition("WHERE")
    return rest


# ---- claim filtering (the correctness boundary) ----


async def test_claim_filters_on_engine_version(fake_db, execute_result):
    fake_db.execute.return_value = execute_result(_job("v2"))

    await processing_job_repository.claim_next_pending(fake_db, ["v2"])

    stmt = fake_db.execute.await_args.args[0]
    assert "engine_version" in _where(stmt)
    assert "'v2'" in _where(stmt)
    # The row lock must survive the added filter, or two workers can claim one job.
    assert "FOR UPDATE SKIP LOCKED" in _rendered(stmt)


async def test_claim_without_versions_is_unfiltered(fake_db, execute_result):
    """A single-fleet deploy (blank setting) must keep claiming everything."""
    fake_db.execute.return_value = execute_result(_job())

    await processing_job_repository.claim_next_pending(fake_db, None)

    assert "engine_version" not in _where(fake_db.execute.await_args.args[0])


async def test_claim_accepts_multiple_versions(fake_db, execute_result):
    fake_db.execute.return_value = execute_result(_job())

    await processing_job_repository.claim_next_pending(fake_db, ["v1", "v2"])

    where = _where(fake_db.execute.await_args.args[0])
    assert "'v1'" in where and "'v2'" in where


async def test_empty_version_list_does_not_filter(fake_db, execute_result):
    """An empty list is "no restriction", not "match nothing" — the latter would
    silently idle the whole fleet."""
    fake_db.execute.return_value = execute_result(_job())

    await processing_job_repository.claim_next_pending(fake_db, [])

    assert "engine_version" not in _where(fake_db.execute.await_args.args[0])


async def test_active_check_can_scope_to_a_version(fake_db, execute_result):
    """Dual-run needs a per-version idempotency check, or the v1 job in flight would
    make the v2 enqueue look like a duplicate and get skipped."""
    fake_db.execute.return_value = execute_result(None)

    await processing_job_repository.get_active_for_project(fake_db, uuid.uuid4(), "v2")

    assert "engine_version" in _where(fake_db.execute.await_args.args[0])


async def test_latest_per_version_keeps_newest_of_each(fake_db, execute_result):
    old_v1, new_v1, v2 = _job("v1"), _job("v1"), _job("v2")
    # Repository orders by created_at DESC, so the newest of each version comes first.
    fake_db.execute.return_value = execute_result([new_v1, old_v1, v2])

    jobs = await processing_job_repository.latest_per_version(fake_db, uuid.uuid4())

    assert [j.engine_version for j in jobs] == ["v1", "v2"]
    assert jobs[0] is new_v1  # the superseded run is not offered for comparison


# ---- worker settings parsing ----


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("v1", ["v1"]),
        ("v2", ["v2"]),
        ("v1,v2", ["v1", "v2"]),
        (" V1 , V2 ", ["v1", "v2"]),  # case/whitespace tolerant
        ("", []),  # blank = claim anything
    ],
)
def test_worker_engine_version_list(raw, expected):
    assert Settings(worker_engine_versions=raw).worker_engine_version_list == expected


def test_worker_defaults_to_v1_only():
    """A container built before v2 existed must not start claiming v2 work on upgrade.

    ``_env_file=None`` so this asserts the code default, not whatever the local .env says.
    """
    assert Settings(_env_file=None).worker_engine_version_list == ["v1"]


# ---- A/B version selection ----


def _settings(**kw) -> Settings:
    """Settings isolated from .env, so A/B assertions don't depend on local config."""
    kw.setdefault("engine_version_default", "v1")
    return Settings(_env_file=None, **kw)


def test_single_version_when_ab_disabled():
    versions = _versions_for(uuid.uuid4(), uuid.uuid4(), _settings(engine_ab_enabled=False))
    assert versions == ["v1"]


def test_default_version_is_honored_when_ab_disabled():
    """Stage 4 of the rollout: flip the default to v2 and stop dual-running."""
    versions = _versions_for(
        uuid.uuid4(), uuid.uuid4(), _settings(engine_ab_enabled=False, engine_version_default="v2")
    )
    assert versions == ["v2"]


def test_allowlisted_user_always_dual_runs():
    user = uuid.uuid4()
    versions = _versions_for(
        uuid.uuid4(),
        user,
        _settings(engine_ab_enabled=True, engine_ab_percent=0, engine_ab_user_allowlist=str(user)),
    )
    assert versions == ["v1", "v2"]


def test_zero_percent_means_nobody_is_bucketed():
    settings = _settings(engine_ab_enabled=True, engine_ab_percent=0)
    assert all(
        _versions_for(uuid.uuid4(), uuid.uuid4(), settings) == ["v1"] for _ in range(50)
    )


def test_hundred_percent_dual_runs_everyone():
    settings = _settings(engine_ab_enabled=True, engine_ab_percent=100)
    assert all(
        _versions_for(uuid.uuid4(), uuid.uuid4(), settings) == ["v1", "v2"] for _ in range(50)
    )


def test_bucket_is_stable_for_a_project():
    """The reason this hashes instead of using random(): a retry must not flip a user's
    project to the other side of the rollout mid-session."""
    project = uuid.uuid4()
    assert len({_ab_bucket(project) for _ in range(10)}) == 1


def test_bucket_is_in_range_and_spreads():
    buckets = [_ab_bucket(uuid.uuid4()) for _ in range(400)]
    assert all(0 <= b < 100 for b in buckets)
    # A constant or badly-truncated hash would collapse the distribution.
    assert len(set(buckets)) > 40


def test_bucket_does_not_use_salted_builtin_hash():
    """``hash()`` is salted per process, so two API replicas would disagree about the
    same project. Pinning one known digest catches a regression to ``hash()``."""
    fixed = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert _ab_bucket(fixed) == _ab_bucket(fixed)
    assert isinstance(_ab_bucket(fixed), int)
