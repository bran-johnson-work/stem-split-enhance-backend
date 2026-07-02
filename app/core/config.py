from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_env: str = "local"
    cors_origins: str = "*"

    # Supabase
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""

    # PostgreSQL — direct URL for Alembic migrations (port 5432)
    database_url: str = ""
    # Pooled URL for the app via PgBouncer (port 6543)
    database_url_pooled: str = ""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def is_local(self) -> bool:
        return self.app_env.lower() == "local"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def active_database_url(self) -> str:
        """App always uses the pooled connection; Alembic uses database_url directly."""
        return self.database_url_pooled or self.database_url


@lru_cache
def get_settings() -> Settings:
    return Settings()
