from functools import lru_cache
from urllib.parse import quote_plus
from uuid import uuid4

from pydantic_settings import BaseSettings, SettingsConfigDict

# Historical insecure placeholder — treated as "unset" by the production guard.
INSECURE_DEFAULT_SECRET = "your-super-secret-key-change-in-production"


class Settings(BaseSettings):
    app_env: str = "local"
    cors_origins: str = "*"
    log_level: str = "INFO"

    # Legacy — nothing reads these; kept so an existing .env still loads.
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""

    # --- AWS S3 object storage ---
    aws_region: str = "us-east-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    # Empty => real AWS; set to an S3-compatible endpoint (LocalStack: :4566) for local dev.
    aws_endpoint_url: str = ""
    # S3 bucket names are globally unique across all AWS accounts — a real deploy suffixes these.
    s3_bucket_inputs: str = "audio-inputs"
    s3_bucket_stems: str = "audio-stems"
    # Profile avatars. Kept private like the others; served via short-lived signed
    # GET URLs surfaced on ``UserResponse.avatar_url``. Blank => reuse the inputs
    # bucket (avatars live under an ``avatars/`` key prefix), so no separate bucket
    # has to be created/CORS-configured — most deploys point every S3_BUCKET_* at
    # one real bucket. Set this only to isolate avatars in their own bucket.
    s3_bucket_avatars: str = ""
    s3_upload_url_expire_seconds: int = 3600
    # Avatar download URL lifetime. Longer than an upload URL because it rides on
    # the (cacheable) user object; 7 days is also the SigV4 presigned-URL maximum.
    avatar_url_expire_seconds: int = 7 * 24 * 3600
    # Per-avatar size ceiling, baked into the presign policy (mirrors the frontend
    # AvatarStore's 2 MB limit).
    max_avatar_size_bytes: int = 2 * 1024 * 1024
    allowed_image_content_types: str = "image/png,image/jpeg,image/webp"

    # --- PostgreSQL (AWS RDS, or any standalone Postgres) ---
    # Set postgres_host to make these take precedence over the legacy DATABASE_URL(_POOLED).
    postgres_type: str = "postgres"
    postgres_host: str = ""
    postgres_port: int = 5432
    postgres_username: str = ""
    postgres_password: str = ""
    postgres_database: str = ""
    # Applies to whichever connection is active, not just the RDS one — see asyncpg_connect_args.
    postgres_ssl: bool = False
    postgres_log: bool = False

    # Legacy Supabase strings, used only when postgres_host is unset: direct (Alembic),
    # pooled = PgBouncer transaction pooler (the app).
    database_url: str = ""
    database_url_pooled: str = ""

    # JWT authentication
    secret_key: str = ""
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "ai-audio-stem-api"
    jwt_audience: str = "ai-audio-stem-api-clients"
    jwt_email_verification_audience: str = "ai-audio-stem-api-email-verification"
    access_token_expire_minutes: int = 30
    # Refresh-token lifetime. The access token stays short (a stolen one expires
    # fast); this is what actually keeps a user signed in, since the client
    # silently exchanges it for a new access token. 30 days of inactivity ends
    # the session — every refresh issues a new token, so an active user's window
    # keeps moving forward.
    refresh_token_expire_days: int = 30

    # PBKDF2-HMAC-SHA256. OWASP-recommended floor is ~600k.
    pbkdf2_iterations: int = 600_000

    # Login brute-force throttle (per client IP + email, sliding window)
    login_rate_limit_max_attempts: int = 5
    login_rate_limit_window_seconds: int = 300


    signup_rate_limit_max_attempts: int = 10
    signup_rate_limit_window_seconds: int = 3600


    # Fallback only, for when a user's plan can't be resolved. NOT a clamp — the presign
    # policy normally carries the plan's own max_file_size_mb, and clamping here would cut
    # every paid tier down to this Supabase-era 50 MB figure.
    max_upload_size_bytes: int = 50 * 1024 * 1024
    allowed_audio_content_types: str = (
        "audio/mpeg,audio/mp3,audio/wav,audio/x-wav,audio/wave,"
        "audio/flac,audio/x-flac,audio/mp4,audio/aac,audio/ogg,audio/webm"
    )

    # Email (Resend). Empty key => sends become logged no-ops, keeping dev/tests network-free.
    resend_api_key: str = ""
    resend_api_base_url: str = "https://api.resend.com"

    email_from: str = "onboarding@resend.dev"
    email_from_name: str = "AI Audio Stem"
    email_timeout_seconds: float = 10.0
    frontend_url: str = "http://localhost:3000"
    email_verification_token_expire_hours: int = 24

    require_email_verification: bool = True

    # --- Billing (Stripe) ---
    # Empty secret key => billing disabled: checkout/portal 503, webhook 404.
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_api_base_url: str = "https://api.stripe.com"
    stripe_timeout_seconds: float = 15.0
    # Empty => fall back to frontend_url.
    billing_success_url: str = ""
    billing_cancel_url: str = ""
    billing_portal_return_url: str = ""

    # --- Stem-separation engine + worker ---
    # auto = CUDA > MPS > CPU. An explicit "cuda"/"cuda:N" REQUIRES the GPU and fails fast
    # at startup rather than silently falling back to CPU (10-50x slower) on a rented box.
    worker_device: str = "auto"
    demucs_model: str = "htdemucs_ft"
    instruments_model: str = "htdemucs_6s"
    enable_instruments: bool = True
    enable_enhancement: bool = True
    worker_poll_interval_seconds: float = 5.0

    # --- Progress-write debounce (RCA-3) ---
    # Sample every poll_seconds, but only write on a label change, a jump >= min_delta,
    # or once min_interval_seconds has passed (liveness tick, so long stages don't look frozen).
    worker_progress_poll_seconds: float = 2.0
    worker_progress_min_delta: float = 5.0
    worker_progress_min_interval_seconds: float = 10.0

    # --- Worker lease / stuck-job recovery (RCA-1) ---
    # The reaper reclaims any PROCESSING job whose heartbeat is older than the stale timeout.
    # Keep that timeout several x the heartbeat interval so a missed beat isn't a false reclaim.
    worker_heartbeat_interval_seconds: float = 15.0
    job_stale_timeout_seconds: float = 90.0
    reaper_interval_seconds: float = 30.0
    # Safe to leave on everywhere (the sweep locks rows SKIP LOCKED).
    worker_run_reaper: bool = True
    job_scratch_dir: str = ""
    # When set, stems are written here instead of uploaded, mirroring the bucket layout:
    # <dir>/<user_id>/<project_id>/stems/<name>.wav.
    stems_local_dir: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def is_local(self) -> bool:
        return self.app_env.lower() == "local"

    @property
    def has_secure_secret(self) -> bool:
        return bool(self.secret_key) and self.secret_key != INSECURE_DEFAULT_SECRET

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def allowed_audio_content_type_set(self) -> set[str]:
        return {t.strip().lower() for t in self.allowed_audio_content_types.split(",") if t.strip()}

    @property
    def allowed_image_content_type_set(self) -> set[str]:
        return {t.strip().lower() for t in self.allowed_image_content_types.split(",") if t.strip()}

    @property
    def has_postgres_components(self) -> bool:
        """True when discrete POSTGRES_* settings drive the connection (AWS RDS)."""
        return bool(self.postgres_host)

    def _build_postgres_url(self) -> str:
        """Assemble a SQLAlchemy async URL from the discrete POSTGRES_* components.

        User and password are percent-encoded so an ``@`` or ``/`` in the password
        can't corrupt the URL.
        """
        user = quote_plus(self.postgres_username)
        password = quote_plus(self.postgres_password)
        auth = f"{user}:{password}@" if user else ""
        return (
            f"postgresql+asyncpg://{auth}"
            f"{self.postgres_host}:{self.postgres_port}/{self.postgres_database}"
        )

    @property
    def active_database_url(self) -> str:
        """URL the app connects with: RDS components, else the legacy pooled/direct URL."""
        if self.has_postgres_components:
            return self._build_postgres_url()
        return self.database_url_pooled or self.database_url

    @property
    def migration_database_url(self) -> str:
        """URL Alembic connects with. Same as the app on RDS; prefers the direct URL for DDL."""
        if self.has_postgres_components:
            return self._build_postgres_url()
        return self.database_url or self.database_url_pooled

    @property
    def db_echo(self) -> bool:
        """Whether SQLAlchemy should echo SQL (explicit POSTGRES_LOG, or local dev)."""
        return self.postgres_log or self.is_local

    def asyncpg_connect_args(self) -> dict:
        """connect_args for asyncpg, shared by the app engine and Alembic.

        The Supabase transaction pooler shares a connection per transaction, so it can't
        support asyncpg's default server-side prepared statements — reused connections
        collide on names like ``__asyncpg_stmt_1__``. Do not remove that branch. A direct
        RDS connection (or RDS Proxy, which pins per session) needs none of it.
        """
        args: dict = {}
        if self.postgres_ssl:
            args["ssl"] = True
        if "pooler.supabase.com" in self.active_database_url:
            args.update(
                statement_cache_size=0,
                prepared_statement_cache_size=0,
                prepared_statement_name_func=lambda: f"__asyncpg_{uuid4()}__",
            )
        return args

    @property
    def email_enabled(self) -> bool:
        """Whether outbound email is configured (a Resend API key is present)."""
        return bool(self.resend_api_key)

    @property
    def billing_enabled(self) -> bool:
        """Whether Stripe billing is configured (a secret key is present)."""
        return bool(self.stripe_secret_key)

    @property
    def checkout_success_url(self) -> str:
        return self.billing_success_url or f"{self.frontend_url}/billing/success"

    @property
    def checkout_cancel_url(self) -> str:
        return self.billing_cancel_url or f"{self.frontend_url}/billing"

    @property
    def portal_return_url(self) -> str:
        return self.billing_portal_return_url or f"{self.frontend_url}/billing"

    @property
    def email_uses_sandbox_sender(self) -> bool:
        """True when EMAIL_FROM is Resend's shared sandbox address.

        With it, Resend only delivers to the account owner (others get HTTP 403), so
        verification emails to real signups silently never arrive.
        """
        return self.email_from.strip().lower() == "onboarding@resend.dev"

    @property
    def email_sender(self) -> str:
        """Sender in the ``Name <address>`` form the Resend API expects."""
        if self.email_from_name:
            return f"{self.email_from_name} <{self.email_from}>"
        return self.email_from

    def assert_production_ready(self) -> None:
        """Fail fast on insecure configuration outside local development.

        Called at startup (``app.main``) rather than import time so tests can still
        construct ``Settings`` for other envs.
        """
        if self.is_local:
            return
        if not self.has_secure_secret:
            raise RuntimeError(
                "SECRET_KEY must be set to a strong, unique value in non-local "
                "environments (it is unset or still the insecure default)."
            )
        if "*" in self.cors_origin_list:
            raise RuntimeError(
                "CORS_ORIGINS must list explicit origins in non-local environments; "
                "the '*' wildcard is not allowed in production."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
