"""Provision the Supabase Storage buckets this API depends on.

Idempotent: creates the ``audio-inputs`` and ``audio-stems`` buckets if they
are missing, otherwise updates them to match the configured limits. Both are
kept **private** — downloads go through short-lived signed URLs.

Run after setting SUPABASE_SERVICE_ROLE_KEY in .env:

    python -m scripts.init_storage
"""

from storage3.exceptions import StorageApiError

from app.core import storage
from app.core.config import get_settings

# Printed when Supabase refuses the requested bucket file_size_limit because it
# exceeds the project-wide global upload cap (Free plan is capped at 50 MB).
_SIZE_CAP_HINT = (
    "Supabase rejected a {mb} MB bucket limit: it exceeds this project's GLOBAL "
    "storage upload limit.\n"
    "  Fix (to support paid tiers): Dashboard -> Storage -> Settings -> 'Upload "
    "file size limit' and raise it. The Free plan is capped at 50 MB; a larger "
    "limit needs a paid Supabase plan. Then re-run this script.\n"
    "  Or (to unblock now): set MAX_UPLOAD_SIZE_BYTES in .env to a value within "
    "the current cap (e.g. 52428800 for 50 MB)."
)


def main() -> None:
    settings = get_settings()
    mb = settings.max_upload_size_bytes // (1024 * 1024)

    # Original uploads: private, size-capped, restricted to audio MIME types.
    try:
        storage.ensure_bucket(
            storage.BUCKET_INPUTS,
            public=False,
            file_size_limit=settings.max_upload_size_bytes,
            allowed_mime_types=sorted(settings.allowed_audio_content_type_set),
        )
    except StorageApiError as exc:
        if getattr(exc, "status", None) == 413 or "maximum allowed size" in str(
            getattr(exc, "message", exc)
        ).lower():
            raise SystemExit(f"[error] {_SIZE_CAP_HINT.format(mb=mb)}") from exc
        raise
    print(f"[ok] bucket ready: {storage.BUCKET_INPUTS} (limit {mb} MB)")

    # Engine-produced stems: private. The engine controls format/size, so no
    # MIME restriction; still served via signed URLs only.
    storage.ensure_bucket(storage.BUCKET_STEMS, public=False)
    print(f"[ok] bucket ready: {storage.BUCKET_STEMS}")


if __name__ == "__main__":
    main()
