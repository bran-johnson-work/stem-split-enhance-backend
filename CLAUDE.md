# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

AI Audio Stem Separation API — a FastAPI backend (package-mode-off Poetry project, Python >=3.11) backed by Supabase (Postgres + Storage). Users upload audio, projects go through async processing jobs, and the engine produces stem output files (vocals, drums, bass, etc.). Billing is Stripe-based (`billing_events`, `plans`, `subscriptions`).

The actual stem-separation engine lives outside this repo (`stem-engine/` is gitignored — "pending migration to root"); this repo is the API/data layer only.

## Commands

```bash
# Install deps (Poetry-managed pyproject, but requirements.txt is kept in sync for pip installs)
pip install -r requirements.txt

# Run the dev server (reload enabled)
python -m app.main
# or
uvicorn app.main:app --reload

# Lint
ruff check .

# Tests (pytest + pytest-asyncio, asyncio_mode=auto). The suite runs with no
# database or network — the async session is mocked and services are patched.
pytest
pytest tests/test_user_service.py::test_authenticate_returns_user_on_success  # single test

# Migrations (Alembic, async, driven by app.core.config settings)
alembic revision --autogenerate -m "description"
alembic upgrade head
```

Environment variables are loaded from `.env` (see `.env.example`). Key ones: the **`POSTGRES_*` component set** (`POSTGRES_HOST` / `POSTGRES_PORT` / `POSTGRES_USERNAME` / `POSTGRES_PASSWORD` / `POSTGRES_DATABASE`, plus `POSTGRES_SSL` for RDS TLS and `POSTGRES_LOG` for SQL echo) — when `POSTGRES_HOST` is set the app **and** Alembic build their async URL from these (see `Settings.active_database_url` / `migration_database_url` / `asyncpg_connect_args`); the legacy `DATABASE_URL` (direct, 5432) / `DATABASE_URL_POOLED` (PgBouncer, 6543) strings are only a fallback when `POSTGRES_HOST` is blank. `SECRET_KEY` (JWT signing — **required in non-local envs; the app refuses to boot without it** via `Settings.assert_production_ready()` in `app/main.py`). Object storage config is `AWS_*` / `S3_*` (see the S3 section); the old `SUPABASE_*` keys are vestigial.

## Containers

Two images, built from this one repo, deployed separately:

- **`Dockerfile.api`** — the FastAPI app (`app.main:app` via uvicorn). Lean stack (no torch); multi-stage build into `/opt/venv`, `python:3.11-slim` runtime (from ECR Public — see below), non-root, stdlib `/health` HEALTHCHECK. COPYs only the API import closure: `main.py`, `api`, `core`, `models`, `repositories`, `schemas`, `services`.
- **`Dockerfile.worker`** — the separation worker (`python -m app.worker`). Carries the torch/demucs stack; also multi-stage/slim/non-root. COPYs only the worker import closure: `core`, `models`, `repositories`, `schemas`, `engine`, `worker` (no `api`/`services`/`main.py`).

The two COPY sets are the **verified** import closures (`app` is a PEP 420 namespace package, no `app/__init__.py`). When a package gains a new cross-layer import, update the relevant Dockerfile's COPY list. `.dockerignore` keeps the build context minimal and, crucially, excludes `.env`.

**Base images come from ECR Public, not Docker Hub** — anonymous Docker Hub pulls are rate limited (100/6h per IP), which breaks CI and fresh hosts. Both Dockerfiles use `ARG PYTHON_IMAGE=public.ecr.aws/docker/library/python:3.11-slim`, AWS's [Docker Official Images mirror](https://gallery.ecr.aws/docker/library/python), which serves the **identical manifest digest** as `docker.io/library/python:3.11-slim` — a registry swap, not an image change. Pulls are free and unmetered from inside an AWS region (10 pulls/sec); from outside, 500 GB/month anonymous. Override the registry with `--build-arg PYTHON_IMAGE=...`.

Neither Dockerfile carries a `# syntax=` directive, deliberately: it would pull the BuildKit frontend from Docker Hub on every build (the exact limit being avoided) and has no ECR Public mirror. Nothing in either file needs a non-default frontend — don't re-add it without a reason.

**CPU vs GPU worker.** The worker defaults to CPU wheels. For an AWS GPU host, build the CUDA variant by overriding the torch wheel index to match the instance driver, then run with the NVIDIA runtime and require the GPU:
```bash
docker build -f Dockerfile.worker \
  --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu126 \
  -t stem-worker:gpu .
docker run --gpus all -e WORKER_DEVICE=cuda ...  stem-worker:gpu
```
torch 2.11 ships on **cu126 / cu128** (not cu121/cu124, which stopped at torch 2.5) — pick the index matching the host driver. Modern torch CUDA wheels bundle the CUDA runtime, so a `slim` base + host driver (via `nvidia-container-toolkit`) suffices — no `nvidia/cuda` devel base needed. **Device selection** is `WORKER_DEVICE` (`Settings.worker_device`, honored in `app/engine/dsp.py::_pick_device`): `auto` picks CUDA > MPS > CPU; an explicit `cuda`/`cuda:N`/`mps` is *required* and raises at startup if unavailable (so a rented GPU box never silently falls back to CPU).

## Architecture

Layered structure, each layer only talks to the one below it:

```
api/v1/endpoints/*.py  → services/*.py  → repositories/*.py  → models/*.py
        ↑                                                          ↑
   schemas/*.py (request/response DTOs)                  core/database.py (Base, engine, get_db)
```

- **`app/models/`** — SQLAlchemy 2.0 declarative models (`Mapped[...]` / `mapped_column`). All models inherit `Base` + `TimestampMixin` from `app/models/base.py`, and use UUID primary keys via `new_uuid()`. `app/models/__init__.py` is the single import point that registers every model on `Base.metadata` — Alembic's `env.py` imports `app.models` (not individual model files) specifically so autogenerate sees the full schema. When adding a new model, add it to `models/__init__.py`'s `__all__` too.
- **`app/repositories/`** — data access only, no business logic. `BaseRepository[ModelType, CreateSchemaType, UpdateSchemaType]` (generic CRUD via `select`/`flush`/`refresh`) is subclassed per model (e.g. `UserRepository`) for model-specific queries. Repos are exported as singletons (`user_repository = UserRepository(User)`) and imported directly rather than injected.
- **`app/services/`** — business logic (password hashing, uniqueness checks, orchestrating repo calls). Services own the `db.commit()` call; repositories only `flush()`/`refresh()`. Also exported as singletons (`user_service = UserService()`).
- **`app/schemas/`** — Pydantic v2 models for API I/O (`*Create`, `*Update`, `*Response`, using `ConfigDict(from_attributes=True)` to serialize from ORM objects). Keep separate from `app/models/` (ORM) — never return ORM objects directly from an endpoint without a response_model schema.
- **`app/api/v1/endpoints/`** — thin route handlers: validate via schema, call the service, translate `ValueError`/`None` into `HTTPException`. Registered onto `api_router` in `app/api/v1/router.py`, mounted at `/api/v1` in `app/main.py`.
- **`app/api/dependencies.py`** — shared FastAPI dependencies. `get_token_payload` decodes + fully validates the JWT (signature, `exp`/`nbf`, `iss`, `aud`, required claims); `get_current_user` builds on it, rejects revoked tokens (logout denylist), and loads the user through `user_repository`.
- **Auth security model** — JWTs carry a full claim set (`sub`/`iat`/`nbf`/`exp`/`jti`/`iss`/`aud`) created in `app/core/security.py`. Logout revokes a token by inserting its `jti` into the `revoked_tokens` table (`RevokedToken` model + `revoked_token_repository`, committed via `auth_service.logout`); `get_current_user` checks that denylist on every request. Login is throttled per client-IP+email by an in-process `SlidingWindowRateLimiter` (`app/core/rate_limit.py`) — swap for a shared store (Redis) for multi-instance. Login/register return generic messages (no account enumeration); the real reason is logged. Auth TTL, PBKDF2 iteration count, issuer/audience, and rate-limit thresholds are all `Settings`-driven.
- **Sessions are access + refresh token pairs.** The access token stays short (`ACCESS_TOKEN_EXPIRE_MINUTES`, 30 by default) — **do not raise it to "fix" logouts**; that weakens security without helping, because the client silently exchanges a refresh token at `POST /auth/refresh` instead. Refresh tokens are **opaque random strings, not JWTs** (a JWT cannot be revoked), stored only as a SHA-256 hash in `refresh_tokens`, and **single-use**: each refresh rotates to a new token and links the old row via `replaced_by_id`. Presenting an already-rotated token is treated as theft and revokes every live token for that user (`auth_service.rotate_refresh_token`) — so a client **must** persist the new refresh token from each response, and concurrent refreshes must be single-flighted or they will log the user out. Logout revokes both the access `jti` and the refresh token. `RefreshError` always surfaces as a generic 401 so failure modes aren't distinguishable.
- **`app/core/`** — cross-cutting concerns: `config.py` (`pydantic-settings` `Settings`, cached via `get_settings()`), `database.py` (async engine + `get_db` session dependency, always using the *pooled* URL for the app), `security.py` (PBKDF2-HMAC-SHA256 password hashing + PyJWT token creation — not passlib/python-jose), `storage.py` (S3 client for the `audio-inputs` / `audio-stems` buckets). 

- **Object storage is AWS S3** (`app/core/storage.py`, boto3), while Postgres stays on Supabase. Local dev runs against **LocalStack** with no AWS account: set `AWS_ENDPOINT_URL=http://localhost:4566`, `docker run --rm -p 4566:4566 localstack/localstack`, then `python -m scripts.init_storage`. Leaving `AWS_ENDPOINT_URL` empty targets real AWS — that env var is the only difference. When the endpoint is set, storage.py forces path-style addressing (virtual-host style would resolve `<bucket>.localhost`).
- **Uploads are presigned POSTs, not PUTs.** An S3 bucket has no `file_size_limit`/`allowed_mime_types` the way a Supabase bucket did, so those constraints live in the presign policy (`content-length-range` + an exact `Content-Type` condition) and S3 rejects violating uploads itself. Clients must send `multipart/form-data` with every returned `fields` entry before the `file` part. `complete_upload` still re-checks the real stored size — a URL can outlive a plan downgrade.
- `tests/test_storage.py` exercises the real boto3 calls against `moto`'s in-process S3 (no network, no account); the rest of the suite mocks `app.core.storage` wholesale.

### Audio restoration (separate from stem separation)

A **second, unrelated pipeline**: separation splits one track into stems; restoration repairs and enhances one piece of audio. It runs on an external GPU service (the `separate-steam-and-enhance-audio` repo) that this backend **proxies** — that service has no CORS and authenticates with a static `X-API-Key`, so it is not browser-callable and the key must never reach the frontend.

- **Two stages, chained.** `v2` = localized MIDI repair; `v3` = tempo/denoise/bandwidth. They are **separate upstream jobs and separate `restoration_jobs` rows**, because V3's *input is V2's output file* — V3 cannot be submitted until V2 has finished and written it. The caller chains them: start `v2`, poll to `done`, then start `v3` with `after_job_id` set to the V2 row. Omitting `after_job_id` runs V3 straight off the original (no repair stage). Note the upstream's own `/v3/restore` runs with `do_stage_a=False` and **never** applies V2 — the chain is what makes a run V2-then-V3.
- **Audio never round-trips through an API process.** Both services exchange S3 keys: the browser uploads via the normal presigned POST, then `restoration_service` submits `{source_key, source_bucket, key_prefix}` to the upstream's `*-from-s3` endpoints. `source_bucket` matters — inputs live in `S3_BUCKET_INPUTS` but the upstream defaults to its own results bucket, so omitting it looks in the wrong place. `key_prefix` is `{user_id}/{project_id}/restored` so results are addressable by owner.
- **`restoration_jobs` exists because the upstream is not durable.** It keeps jobs in memory, evicts past 200, and loses all of them on restart. A poll that 404s upstream marks the row FAILED rather than spinning forever; a row already `done` is never re-fetched, or a restart would trade a stored result for a 404.
- **Output URLs are signed per request** in `to_response()`, never stored — the upstream's are 1h and would be dead on page reload.
- Progress is a **step log (`list[str]`), not a percentage** — the pipeline does not emit one, so the UI shows the latest label instead of a bar.
- Config is `RESTORATION_API_URL` / `RESTORATION_API_KEY`; blank URL disables the feature and every endpoint answers 503. `GET /api/v1/restoration/status` reports reachability, CUDA, and whether the Apollo checkout exists (bandwidth extension raises without it).
- `tests/test_restoration.py` stubs the upstream on an httpx `MockTransport`, so the real request/response and error-translation paths run with no GPU host.

### Conventions to follow when extending this codebase

- New resources follow the full vertical slice: model → repository → schema → service → endpoint → router registration.
- Repository/service instances are module-level singletons, not constructed per-request or injected via `Depends`.
- Use timezone-aware UUID primary keys and the `TimestampMixin` for any new table.
- Enum-like status columns (`ProjectStatus`, `JobStatus`, `SubscriptionStatus`) are Python `str` enums stored via SQLAlchemy `Enum`.
- Migrations are async (see `alembic/env.py`); it builds its URL from `Settings.migration_database_url` (RDS components, or the legacy direct URL) and passes `connect_args={"ssl": True}` when `POSTGRES_SSL` is set. On the legacy Supabase-pooler fallback, `prepared_statement_cache_size=0` is appended to the URL automatically.
- **asyncpg connect args are centralized** in `Settings.asyncpg_connect_args()` (shared by `app/core/database.py` and Alembic). On AWS RDS it just carries TLS (`ssl=True` when `POSTGRES_SSL`). The Supabase *transaction pooler* (PgBouncer, port 6543) cannot support asyncpg's default server-side prepared statements, so for `pooler.supabase.com` URLs the helper disables the statement caches and assigns unique prepared-statement names — do not remove this branch, or reused pooled connections will fail with `DuplicatePreparedStatementError`. A direct RDS connection (or RDS Proxy, which pins per session) doesn't need it.
- `ruff` is configured for `E`, `F`, `I` rule sets with a 100-char line length (`pyproject.toml`).

### Reference skills

`.agents/skills/` (gitignored, local-only) contains the FastAPI architecture template this codebase follows (`fastapi-architecture/`) and Supabase-specific guidance (`supabase/`, `supabase-postgres-best-practices/`) — consult these for canonical patterns (e.g. RLS checklist, connection pooling, indexing) before making changes that touch Supabase or auth.

alembic upgrade head
python -m scripts.create_admin --email admin@example.com --password 'Admin123!' --name "Local Admin"