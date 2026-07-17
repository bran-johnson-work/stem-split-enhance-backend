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

Environment variables are loaded from `.env` (see `.env.example`). Key ones: `DATABASE_URL` (direct, port 5432 — used only by Alembic), `DATABASE_URL_POOLED` (PgBouncer, port 6543 — used by the app), `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` (storage), `SECRET_KEY` (JWT signing — **required in non-local envs; the app refuses to boot without it** via `Settings.assert_production_ready()` in `app/main.py`).

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
- **`app/core/`** — cross-cutting concerns: `config.py` (`pydantic-settings` `Settings`, cached via `get_settings()`), `database.py` (async engine + `get_db` session dependency, always using the *pooled* URL for the app), `security.py` (PBKDF2-HMAC-SHA256 password hashing + PyJWT token creation — not passlib/python-jose), `storage.py` (S3 client for the `audio-inputs` / `audio-stems` buckets). 

- **Object storage is AWS S3** (`app/core/storage.py`, boto3), while Postgres stays on Supabase. Local dev runs against **LocalStack** with no AWS account: set `AWS_ENDPOINT_URL=http://localhost:4566`, `docker run --rm -p 4566:4566 localstack/localstack`, then `python -m scripts.init_storage`. Leaving `AWS_ENDPOINT_URL` empty targets real AWS — that env var is the only difference. When the endpoint is set, storage.py forces path-style addressing (virtual-host style would resolve `<bucket>.localhost`).
- **Uploads are presigned POSTs, not PUTs.** An S3 bucket has no `file_size_limit`/`allowed_mime_types` the way a Supabase bucket did, so those constraints live in the presign policy (`content-length-range` + an exact `Content-Type` condition) and S3 rejects violating uploads itself. Clients must send `multipart/form-data` with every returned `fields` entry before the `file` part. `complete_upload` still re-checks the real stored size — a URL can outlive a plan downgrade.
- `tests/test_storage.py` exercises the real boto3 calls against `moto`'s in-process S3 (no network, no account); the rest of the suite mocks `app.core.storage` wholesale.

### Conventions to follow when extending this codebase

- New resources follow the full vertical slice: model → repository → schema → service → endpoint → router registration.
- Repository/service instances are module-level singletons, not constructed per-request or injected via `Depends`.
- Use timezone-aware UUID primary keys and the `TimestampMixin` for any new table.
- Enum-like status columns (`ProjectStatus`, `JobStatus`, `SubscriptionStatus`) are Python `str` enums stored via SQLAlchemy `Enum`.
- Migrations are async (see `alembic/env.py`); when connecting through the Supabase pooler, `prepared_statement_cache_size=0` must be appended to the URL (already handled automatically in `env.py`).
- **Transaction pooler + asyncpg:** the app's engine (`app/core/database.py`) connects through Supabase's transaction pooler (port 6543), which cannot support asyncpg's default server-side prepared statements. `_build_connect_args()` disables the statement caches and assigns unique prepared-statement names for `pooler.supabase.com` URLs — do not remove this, or reused pooled connections will fail with `DuplicatePreparedStatementError`.
- `ruff` is configured for `E`, `F`, `I` rule sets with a 100-char line length (`pyproject.toml`).

### Reference skills

`.agents/skills/` (gitignored, local-only) contains the FastAPI architecture template this codebase follows (`fastapi-architecture/`) and Supabase-specific guidance (`supabase/`, `supabase-postgres-best-practices/`) — consult these for canonical patterns (e.g. RLS checklist, connection pooling, indexing) before making changes that touch Supabase or auth.
