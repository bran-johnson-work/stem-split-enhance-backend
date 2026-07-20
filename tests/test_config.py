import pytest

from app.core.config import INSECURE_DEFAULT_SECRET, Settings


def test_is_local_is_case_insensitive():
    assert Settings(app_env="local").is_local is True
    assert Settings(app_env="LOCAL").is_local is True
    assert Settings(app_env="Local").is_local is True


def test_is_local_false_for_other_envs():
    assert Settings(app_env="production").is_local is False
    assert Settings(app_env="staging").is_local is False


def test_cors_origin_list_splits_and_strips():
    settings = Settings(cors_origins="http://a.com, http://b.com ,http://c.com")
    assert settings.cors_origin_list == ["http://a.com", "http://b.com", "http://c.com"]


def test_cors_origin_list_ignores_empty_entries():
    assert Settings(cors_origins="a,, ,b").cors_origin_list == ["a", "b"]


def test_cors_origin_list_wildcard():
    assert Settings(cors_origins="*").cors_origin_list == ["*"]


def test_active_database_url_prefers_pooled():
    # Legacy fallback path (no POSTGRES_* components): app prefers the pooled URL.
    settings = Settings(database_url="direct-url", database_url_pooled="pooled-url")
    assert settings.active_database_url == "pooled-url"


def test_active_database_url_falls_back_to_direct():
    settings = Settings(database_url="direct-url", database_url_pooled="")
    assert settings.active_database_url == "direct-url"


def test_postgres_components_build_async_url():
    settings = Settings(
        postgres_host="mydb.abc123.us-east-1.rds.amazonaws.com",
        postgres_port=5432,
        postgres_username="postgres",
        postgres_password="s3cret",
        postgres_database="appdb",
    )
    expected = (
        "postgresql+asyncpg://postgres:s3cret@"
        "mydb.abc123.us-east-1.rds.amazonaws.com:5432/appdb"
    )
    assert settings.active_database_url == expected
    # Alembic uses the same single RDS endpoint.
    assert settings.migration_database_url == expected


def test_postgres_components_take_precedence_over_legacy_urls():
    settings = Settings(
        postgres_host="rds-host",
        postgres_username="u",
        postgres_password="p",
        postgres_database="d",
        database_url="legacy-direct",
        database_url_pooled="legacy-pooled",
    )
    assert settings.active_database_url == "postgresql+asyncpg://u:p@rds-host:5432/d"


def test_postgres_password_is_url_encoded():
    settings = Settings(
        postgres_host="rds-host",
        postgres_username="user@corp",
        postgres_password="p@ss/w:rd",
        postgres_database="d",
    )
    # @, / and : in credentials must be percent-encoded, not left to corrupt the URL.
    assert "user%40corp:p%40ss%2Fw%3Ard@rds-host" in settings.active_database_url


def test_asyncpg_connect_args_requests_ssl_when_enabled():
    assert Settings(postgres_host="rds", postgres_ssl=True).asyncpg_connect_args() == {"ssl": True}
    assert Settings(postgres_host="rds", postgres_ssl=False).asyncpg_connect_args() == {}


def test_asyncpg_connect_args_pooler_workaround_only_for_supabase():
    # RDS: no prepared-statement workaround.
    rds = Settings(postgres_host="rds-host").asyncpg_connect_args()
    assert "prepared_statement_name_func" not in rds
    # Legacy Supabase pooler URL: workaround kicks in.
    pooler = Settings(
        database_url_pooled="postgresql+asyncpg://u:p@aws-0-us-east-2.pooler.supabase.com:6543/postgres"
    ).asyncpg_connect_args()
    assert pooler["statement_cache_size"] == 0
    assert pooler["prepared_statement_cache_size"] == 0
    assert callable(pooler["prepared_statement_name_func"])


def test_db_echo_follows_log_flag_or_local():
    assert Settings(app_env="production", postgres_log=True).db_echo is True
    assert Settings(app_env="production", postgres_log=False).db_echo is False
    assert Settings(app_env="local", postgres_log=False).db_echo is True


def test_has_secure_secret():
    assert Settings(secret_key="a-strong-random-value").has_secure_secret is True
    assert Settings(secret_key="").has_secure_secret is False
    assert Settings(secret_key=INSECURE_DEFAULT_SECRET).has_secure_secret is False


def test_production_guard_raises_without_secret():
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        Settings(app_env="production", secret_key="").assert_production_ready()


def test_production_guard_passes_with_secret_and_explicit_cors():
    # Should not raise: strong secret + explicit (non-wildcard) origins.
    Settings(
        app_env="production",
        secret_key="a-strong-random-value",
        cors_origins="https://app.example.com",
    ).assert_production_ready()


def test_production_guard_rejects_wildcard_cors():
    with pytest.raises(RuntimeError, match="CORS_ORIGINS"):
        Settings(
            app_env="production",
            secret_key="a-strong-random-value",
            cors_origins="*",
        ).assert_production_ready()


def test_local_env_skips_production_guard():
    # Even with no secret, local dev is allowed to boot.
    Settings(app_env="local", secret_key="").assert_production_ready()
