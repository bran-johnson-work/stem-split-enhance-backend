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


def create_signed_upload_url(bucket: str, path: str) -> dict[str, str]:
    """Mint a one-time signed URL the client can upload *directly* to.

    Returns ``{"url": ..., "token": ..., "path": ...}``. The file bytes never
    pass through the API and the service_role key stays server-side; the bucket's
    own ``file_size_limit`` / ``allowed_mime_types`` enforce the constraints at
    upload time. The token is single-use and expires (Supabase default ~2h).
    """
    client = get_storage_client()
    response = client.storage.from_(bucket).create_signed_upload_url(path)
    return {
        "url": response["signed_url"],
        "token": response["token"],
        "path": response["path"],
    }


def list_objects(bucket: str, prefix: str) -> list[dict]:
    """List objects stored directly under ``prefix`` (non-recursive)."""
    client = get_storage_client()
    return client.storage.from_(bucket).list(prefix)


def delete_file(bucket: str, path: str) -> None:
    client = get_storage_client()
    client.storage.from_(bucket).remove([path])


def delete_prefix(bucket: str, prefix: str) -> None:
    """Delete every object stored directly under ``prefix`` (non-recursive).

    Used to purge a project's storage when the project (or all its uploads) is
    deleted. No-op when the prefix is already empty.
    """
    client = get_storage_client()
    objects = client.storage.from_(bucket).list(prefix)
    paths = [f"{prefix}/{o['name']}" for o in objects if o.get("name")]
    if paths:
        client.storage.from_(bucket).remove(paths)
