from app.core.config import Settings


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
    settings = Settings(database_url="direct-url", database_url_pooled="pooled-url")
    assert settings.active_database_url == "pooled-url"


def test_active_database_url_falls_back_to_direct():
    settings = Settings(database_url="direct-url", database_url_pooled="")
    assert settings.active_database_url == "direct-url"
