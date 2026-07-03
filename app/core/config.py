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
    access_token_expire_minutes: int = 30

    # Password hashing (PBKDF2-HMAC-SHA256). OWASP-recommended floor is ~600k.
    pbkdf2_iterations: int = 600_000

    # Login brute-force throttle (per client IP + email, sliding window)
    login_rate_limit_max_attempts: int = 5
    login_rate_limit_window_seconds: int = 300

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
    def active_database_url(self) -> str:
        """App always uses the pooled connection; Alembic uses database_url directly."""
        return self.database_url_pooled or self.database_url

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


@lru_cache
def get_settings() -> Settings:
    return Settings()
