from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

# Historical insecure placeholder — treated as "unset" by the production guard.
INSECURE_DEFAULT_SECRET = "your-super-secret-key-change-in-production"


class Settings(BaseSettings):
    app_env: str = "local"
    cors_origins: str = "*"
    log_level: str = "INFO"

    # Supabase
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""

    # PostgreSQL — direct URL for Alembic migrations (port 5432)
    database_url: str = ""
    # Pooled URL for the app via PgBouncer (port 6543)
    database_url_pooled: str = ""

    # JWT authentication
    secret_key: str = ""
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "ai-audio-stem-api"
    jwt_audience: str = "ai-audio-stem-api-clients"
    jwt_email_verification_audience: str = "ai-audio-stem-api-email-verification"
    access_token_expire_minutes: int = 30

    # Password hashing (PBKDF2-HMAC-SHA256). OWASP-recommended floor is ~600k.
    pbkdf2_iterations: int = 600_000

    # Login brute-force throttle (per client IP + email, sliding window)
    login_rate_limit_max_attempts: int = 5
    login_rate_limit_window_seconds: int = 300


    signup_rate_limit_max_attempts: int = 10
    signup_rate_limit_window_seconds: int = 3600

    # Audio uploads. 50 MB matches the Supabase Free-plan global upload ceiling;
    # raise this (and the project's Storage limit) on a paid plan if needed.
    max_upload_size_bytes: int = 50 * 1024 * 1024  # 50 MB
    allowed_audio_content_types: str = (
        "audio/mpeg,audio/mp3,audio/wav,audio/x-wav,audio/wave,"
        "audio/flac,audio/x-flac,audio/mp4,audio/aac,audio/ogg,audio/webm"
    )

    # Email (Resend transactional email API — https://resend.com).
    # ``resend_api_key`` empty => email is disabled (sends become no-ops that log
    # a warning), which keeps the test suite and local dev network-free.
    resend_api_key: str = ""
    resend_api_base_url: str = "https://api.resend.com"

    email_from: str = "onboarding@resend.dev"
    email_from_name: str = "AI Audio Stem"
    email_timeout_seconds: float = 10.0
    # Base URL of the frontend that hosts the email-verification landing page.
    frontend_url: str = "http://localhost:3000"
    # How long an email-verification link stays valid.
    email_verification_token_expire_hours: int = 24

    require_email_verification: bool = True

    # --- Stem-separation engine + worker ---
    # HT-Demucs fine-tuned bag (vocals / drums / bass / other).
    demucs_model: str = "htdemucs_ft"
    # HT-Demucs 6-source model, adds guitar + piano.
    instruments_model: str = "htdemucs_6s"
    # Toggle the optional stages. Instruments = the guitar/piano pass; enhancement = the
    # DSP restoration chain. Turning either off makes jobs faster/cheaper.
    enable_instruments: bool = True
    enable_enhancement: bool = True
    # How often an idle worker polls the queue for the next PENDING job.
    worker_poll_interval_seconds: float = 5.0
    # Scratch dir for a job's temp files; empty => the OS default temp dir.
    job_scratch_dir: str = ""

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
    def active_database_url(self) -> str:
        """App always uses the pooled connection; Alembic uses database_url directly."""
        return self.database_url_pooled or self.database_url

    @property
    def email_enabled(self) -> bool:
        """Whether outbound email is configured (a Resend API key is present)."""
        return bool(self.resend_api_key)

    @property
    def email_uses_sandbox_sender(self) -> bool:
        """True when EMAIL_FROM is Resend's shared sandbox address.

        With this sender Resend only delivers to the address that owns the
        account (any other recipient is rejected with HTTP 403), so verification
        emails to real signups silently never arrive until a domain is verified.
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

        Called at application startup (see ``app.main``) rather than at import
        time so unit tests can still construct ``Settings`` for other envs.
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
