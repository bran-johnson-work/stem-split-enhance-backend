# AI Audio Stem Separation API

A FastAPI backend for audio stem separation with async processing jobs, powered by Supabase (Postgres + Storage) and Stripe billing.

Users upload audio files, the system processes them through stem-separation jobs, and returns individual stem outputs (vocals, drums, bass, etc.).

## Prerequisites

- **Python 3.11+** (for async features and type hints)
- **Poetry** (dependency manager)
- **Supabase account** (Postgres database + Storage)
- **Stripe account** (optional, for billing features)

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
```

Or with Poetry:

```bash
poetry install
```

### 2. Configure Environment Variables

Copy `.env.example` to `.env` and fill in the required values:

```bash
cp .env.example .env
```

Key environment variables:
- `DATABASE_URL` — Direct PostgreSQL connection (port 5432, used by Alembic migrations)
- `DATABASE_URL_POOLED` — PgBouncer pooled connection (port 6543, used by the app)
- `SUPABASE_URL` — Your Supabase project URL
- `SUPABASE_SERVICE_ROLE_KEY` — Supabase service role key for Storage access
- `SECRET_KEY` — JWT signing key (required in non-local environments; app refuses to boot without it in production)
- `STRIPE_SECRET_KEY` — Stripe API key (for billing, if enabled)
- `RESEND_API_KEY` — [Resend](https://resend.com) API key for transactional email. Leave empty to disable outbound email (sends become logged no-ops — handy for local/dev)
- `EMAIL_FROM` / `EMAIL_FROM_NAME` — sender identity. `EMAIL_FROM` must be on a domain verified in Resend; `onboarding@resend.dev` is Resend's sandbox sender (only delivers to your own account address until a domain is verified)
- `FRONTEND_URL` — base URL that hosts the `/verify-email?token=...` landing page

### 3. Run Migrations

Set up the database schema:

```bash
alembic upgrade head
```

Create a new migration after schema changes:

```bash
alembic revision --autogenerate -m "description of changes"
```

### 4. Start the Development Server

```bash
# Using Python module
python -m app.main

# Or using Uvicorn directly
uvicorn app.main:app --reload
```

The API will be available at `http://localhost:8000`

**API Documentation:**
- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

## Development

### Running Tests

Run the full test suite:

```bash
pytest
```

Run a specific test file:

```bash
pytest tests/test_user_service.py
```

Run a specific test:

```bash
pytest tests/test_user_service.py::test_authenticate_returns_user_on_success
```

The test suite runs with a mocked async session and patched services — no real database or network calls.

### Code Quality

Lint your code with Ruff:

```bash
ruff check .
```

Fix linting issues automatically:

```bash
ruff check . --fix
```

Code style is configured for:
- Error codes: `E`, `F`, `I`
- Line length: 100 characters

## Project Structure

```
app/
├── api/v1/
│   ├── endpoints/      # Route handlers (thin, validation + service calls)
│   ├── router.py       # Endpoint registration
│   └── dependencies.py # FastAPI dependencies (auth, etc.)
├── models/             # SQLAlchemy ORM models
├── repositories/       # Data access layer (CRUD operations)
├── services/           # Business logic layer
├── schemas/            # Pydantic request/response DTOs
├── core/
│   ├── config.py       # Settings management
│   ├── database.py     # Database engine and session
│   ├── security.py     # JWT and password hashing
│   └── storage.py      # Supabase Storage client
└── main.py             # FastAPI app entry point

tests/                  # Test suite
alembic/                # Database migrations
```

## Architecture

The codebase follows a layered architecture:

```
API Endpoints
    ↓
Services (business logic)
    ↓
Repositories (data access)
    ↓
Models (ORM)
```

Each layer communicates only with the layer below it. This separation ensures clean boundaries and testability.

### Key Concepts

- **Models** — SQLAlchemy 2.0 declarative ORM with UUID primary keys and timestamp mixins
- **Repositories** — Generic CRUD via `BaseRepository`, no business logic
- **Services** — Business rules, validation, and transaction management (owns `db.commit()`)
- **Schemas** — Pydantic v2 DTOs for API requests/responses
- **Authentication** — JWT-based with revocation (denylist in `revoked_tokens` table)
- **Rate Limiting** — In-process sliding window limiter per IP+email (swap for Redis for multi-instance)
- **Storage** — Supabase Storage for audio files (buckets: `audio-inputs`, `audio-stems`)
- **Email** — Transactional email via [Resend](https://resend.com). `app/core/email.py` is the async transport (no-op when unconfigured); `app/services/email_service.py` composes messages and drives the email-verification flow

### Email & Verification

Registration sends a verification email (fire-and-forget background task — a
failed email never fails registration). Verification links carry a short-lived,
**single-use** JWT scoped to a dedicated audience so it can never be replayed as
an API access token; on confirm, its `jti` is added to the `revoked_tokens`
denylist so the same link cannot be used twice. Expiry is set by
`EMAIL_VERIFICATION_TOKEN_EXPIRE_HOURS` (24h default).

Endpoints (`/api/v1/auth`):
- `POST /verify-email/request` — `{ "email": "..." }` (re)sends the link; always returns a generic message (no account enumeration)
- `POST /verify-email/confirm` — `{ "token": "..." }` marks the address verified (`400` on invalid/expired token)

Verify the integration end-to-end with a real send (needs `RESEND_API_KEY` in `.env`):

```bash
python -m scripts.send_test_email you@example.com
```

While `EMAIL_FROM` is the sandbox sender (`onboarding@resend.dev`), Resend only
delivers to the address that owns your Resend account until you verify a domain.

## Common Tasks

### Create a New Resource

Follow this vertical slice pattern:

1. Create the **model** in `app/models/`
2. Add it to `app/models/__init__.py`
3. Create the **repository** in `app/repositories/`
4. Create the **schema** in `app/schemas/`
5. Create the **service** in `app/services/`
6. Create the **endpoint** in `app/api/v1/endpoints/`
7. Register the endpoint in `app/api/v1/router.py`

### Add a Database Migration

```bash
# Make changes to models in app/models/
alembic revision --autogenerate -m "description"
alembic upgrade head
```

### Add Environment Variables

1. Add to `.env.example`
2. Add to `.env` (keep this file out of git)
3. Add the field to `Settings` in `app/core/config.py`

## Troubleshooting

### Secret Key Missing Error

If you see "secret key not found," set `SECRET_KEY` in `.env`:

```bash
SECRET_KEY=your-secure-random-string-here
```

### Database Connection Issues

Ensure you're using the **pooled** database URL (`DATABASE_URL_POOLED`) in your `.env`:

```
DATABASE_URL_POOLED=postgresql://user:password@pooler.supabase.com:6543/postgres
```

For Alembic, use the **direct** URL:

```
DATABASE_URL=postgresql://user:password@db.supabase.com:5432/postgres
```

### Test Failures

Clear any cached imports and re-run:

```bash
pytest --cache-clear
```

## Additional Resources

- [FastAPI Documentation](https://fastapi.tiangolo.com)
- [SQLAlchemy 2.0 Guide](https://docs.sqlalchemy.org/20/)
- [Pydantic v2 Documentation](https://docs.pydantic.dev/latest/)
- [Alembic Migrations Guide](https://alembic.sqlalchemy.org)
- [Supabase Documentation](https://supabase.com/docs)

## Support

For issues or questions:
1. Check the `CLAUDE.md` file for architecture and convention details
2. Review test files for usage examples
3. Consult Supabase and FastAPI documentation
