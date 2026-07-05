from functools import lru_cache

from supabase import Client, create_client

from app.core.config import get_settings

# Buckets
BUCKET_INPUTS = "audio-inputs"
BUCKET_STEMS = "audio-stems"


@lru_cache
def get_storage_client() -> Client:
    settings = get_settings()
    if not settings.supabase_service_role_key:
        raise RuntimeError(
            "SUPABASE_SERVICE_ROLE_KEY is not set - server-side Storage uploads require the "
            "secret service_role key (Dashboard -> Project Settings -> API)."
        )
    return create_client(settings.supabase_url, settings.supabase_service_role_key)


def ensure_bucket(
    bucket: str,
    *,
    public: bool = False,
    file_size_limit: int | None = None,
    allowed_mime_types: list[str] | None = None,
) -> None:
    """Create the bucket if missing, otherwise update it to match — idempotent.

    Requires the service_role key. Safe to run repeatedly (e.g. at deploy time).
    """
    client = get_storage_client()
    options: dict = {"public": public}
    if file_size_limit is not None:
        options["file_size_limit"] = file_size_limit
    if allowed_mime_types is not None:
        options["allowed_mime_types"] = allowed_mime_types

    existing = {getattr(b, "name", getattr(b, "id", None)) for b in client.storage.list_buckets()}
    if bucket in existing:
        client.storage.update_bucket(bucket, options)
    else:
        client.storage.create_bucket(bucket, options=options)


def upload_file(bucket: str, path: str, data: bytes, content_type: str = "audio/wav") -> str:
    """Upload bytes to a bucket. Returns the storage path."""
    client = get_storage_client()
    client.storage.from_(bucket).upload(
        path=path,
        file=data,
        file_options={"content-type": content_type, "upsert": "true"},
    )
    return path


def get_signed_url(bucket: str, path: str, expires_in: int = 3600) -> str:
    """Generate a short-lived signed download URL."""
    client = get_storage_client()
    response = client.storage.from_(bucket).create_signed_url(path, expires_in)
    return response["signedURL"]


def delete_file(bucket: str, path: str) -> None:
    client = get_storage_client()
    client.storage.from_(bucket).remove([path])
