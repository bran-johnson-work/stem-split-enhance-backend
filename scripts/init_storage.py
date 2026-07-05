"""Provision the Supabase Storage buckets this API depends on.

Idempotent: creates the ``audio-inputs`` and ``audio-stems`` buckets if they
are missing, otherwise updates them to match the configured limits. Both are
kept **private** — downloads go through short-lived signed URLs.

Run after setting SUPABASE_SERVICE_ROLE_KEY in .env:

    python -m scripts.init_storage
"""

from app.core import storage
from app.core.config import get_settings


def main() -> None:
    settings = get_settings()

    # Original uploads: private, size-capped, restricted to audio MIME types.
    storage.ensure_bucket(
        storage.BUCKET_INPUTS,
        public=False,
        file_size_limit=settings.max_upload_size_bytes,
        allowed_mime_types=sorted(settings.allowed_audio_content_type_set),
    )
    print(f"[ok] bucket ready: {storage.BUCKET_INPUTS}")

    # Engine-produced stems: private. The engine controls format/size, so no
    # MIME restriction; still served via signed URLs only.
    storage.ensure_bucket(storage.BUCKET_STEMS, public=False)
    print(f"[ok] bucket ready: {storage.BUCKET_STEMS}")


if __name__ == "__main__":
    main()
