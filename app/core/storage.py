from functools import lru_cache

from supabase import Client, create_client

from app.core.config import get_settings

# Buckets
BUCKET_INPUTS = "audio-inputs"
BUCKET_STEMS = "audio-stems"


@lru_cache
def get_storage_client() -> Client:
    settings = get_settings()
    return create_client(settings.supabase_url, settings.supabase_service_role_key)


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
