"""Provision the S3 buckets this API depends on.

Idempotent: creates the input and stem buckets if they are missing. Both are kept
**private** (public access blocked) — downloads go through short-lived signed URLs.

Unlike Supabase Storage, an S3 bucket carries no ``file_size_limit`` or
``allowed_mime_types``; those are enforced per-upload by the presigned POST policy
minted in ``app.core.storage.create_signed_upload_url``, so there is nothing to
configure on the bucket here.

Run after setting the AWS_* variables in .env:

    python -m scripts.init_storage

Against LocalStack (AWS_ENDPOINT_URL=http://localhost:4566) this needs the
container running; against real AWS it needs credentials with s3:CreateBucket.
"""

from botocore.exceptions import ClientError, EndpointConnectionError

from app.core import storage
from app.core.config import get_settings

_ENDPOINT_HINT = (
    "Could not reach the S3 endpoint at {endpoint}.\n"
    "  If you're using LocalStack, start it first:\n"
    "    docker run --rm -p 4566:4566 localstack/localstack\n"
    "  If you meant to use real AWS, leave AWS_ENDPOINT_URL empty in .env."
)


def main() -> None:
    settings = get_settings()
    target = settings.aws_endpoint_url or f"AWS S3 ({settings.aws_region})"
    print(f"[info] provisioning buckets on {target}")

    try:
        for bucket in (storage.BUCKET_INPUTS, storage.BUCKET_STEMS):
            storage.ensure_bucket(bucket, public=False)
            print(f"[ok] bucket ready: {bucket} (private)")
    except EndpointConnectionError as exc:
        raise SystemExit(
            f"[error] {_ENDPOINT_HINT.format(endpoint=settings.aws_endpoint_url)}"
        ) from exc
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("InvalidAccessKeyId", "SignatureDoesNotMatch", "AccessDenied"):
            raise SystemExit(
                f"[error] S3 rejected the credentials ({code}). Check AWS_ACCESS_KEY_ID / "
                "AWS_SECRET_ACCESS_KEY in .env — LocalStack accepts any non-empty pair."
            ) from exc
        raise

    mb = settings.max_upload_size_bytes // (1024 * 1024)
    print(f"[info] uploads are capped at {mb} MB by the presign policy, not the bucket")


if __name__ == "__main__":
    main()
