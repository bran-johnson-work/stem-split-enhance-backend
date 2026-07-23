"""Object storage on AWS S3 (or any S3-compatible endpoint, e.g. LocalStack).

The public surface here is deliberately the same one the rest of the app already
calls (``upload_file`` / ``download_file`` / ``get_signed_url`` / ``list_objects``
/ ``delete_file`` / ``delete_prefix`` / ``create_signed_upload_url``), so services
and the worker are unaware of the backend behind it.

Pointing at LocalStack vs. real AWS is purely configuration: set
``AWS_ENDPOINT_URL`` (e.g. ``http://localhost:4566``) to use a local simulator,
leave it empty to talk to real S3. Nothing else changes.
"""

import logging
from functools import lru_cache
from typing import Any

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_settings = get_settings()

# Buckets. Names are settings-driven because real S3 bucket names are globally
# unique across all AWS accounts, so a deploy usually needs a suffixed name.
BUCKET_INPUTS = _settings.s3_bucket_inputs
BUCKET_STEMS = _settings.s3_bucket_stems


@lru_cache
def get_storage_client():
    """Build the S3 client. Cached — boto3 clients are thread-safe and reusable.

    When ``aws_endpoint_url`` is set (LocalStack), path-style addressing is forced:
    virtual-host style would resolve ``<bucket>.localhost``, which doesn't exist.
    Real AWS is left on boto3's default (virtual-host) addressing.
    """
    settings = get_settings()
    if not settings.aws_access_key_id or not settings.aws_secret_access_key:
        raise RuntimeError(
            "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are not set - S3 storage requires "
            "credentials. For LocalStack any non-empty value works (conventionally 'test')."
        )

    config_kwargs: dict[str, Any] = {"signature_version": "s3v4"}
    if settings.aws_endpoint_url:
        config_kwargs["s3"] = {"addressing_style": "path"}

    return boto3.client(
        "s3",
        region_name=settings.aws_region,
        aws_access_key_id=settings.aws_access_key_id,
        aws_secret_access_key=settings.aws_secret_access_key,
        endpoint_url=settings.aws_endpoint_url or None,
        config=Config(**config_kwargs),
    )


def ensure_bucket(bucket: str, *, public: bool = False) -> None:
    """Create the bucket if missing — idempotent, safe to run at deploy time.

    Unlike Supabase Storage, an S3 bucket has no ``file_size_limit`` or
    ``allowed_mime_types`` property; those constraints are enforced per-upload by
    the presigned POST policy (see :func:`create_signed_upload_url`) instead of by
    the bucket. Private buckets get an explicit public-access block so a stray ACL
    can't expose objects — downloads go through short-lived signed URLs only.
    """
    client = get_storage_client()
    settings = get_settings()

    try:
        client.head_bucket(Bucket=bucket)
        exists = True
    except ClientError as exc:
        if exc.response["Error"]["Code"] not in ("404", "NoSuchBucket", "403"):
            raise
        exists = False

    if not exists:
        kwargs: dict[str, Any] = {"Bucket": bucket}
        # us-east-1 is the API default and rejects an explicit LocationConstraint.
        if settings.aws_region and settings.aws_region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": settings.aws_region}
        try:
            client.create_bucket(**kwargs)
        except ClientError as exc:
            # Someone else won the race; that's the outcome we wanted anyway.
            if exc.response["Error"]["Code"] not in (
                "BucketAlreadyOwnedByYou",
                "BucketAlreadyExists",
            ):
                raise

    if not public:
        try:
            client.put_public_access_block(
                Bucket=bucket,
                PublicAccessBlockConfiguration={
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                },
            )
        except ClientError:  # pragma: no cover - not all S3 clones implement this
            logger.warning("Could not set public access block on %s", bucket)

    # Browser uploads go straight to S3 via a presigned POST, and downloads/playback
    # hit presigned GET URLs — both are cross-origin requests from the frontend, so
    # the *bucket* needs a CORS policy or real S3 blocks them. (LocalStack doesn't
    # enforce CORS, which is why this only bites in production.)
    put_bucket_cors(bucket, settings.cors_origin_list)


def put_bucket_cors(bucket: str, origins: list[str]) -> None:
    """Configure the bucket's CORS policy for direct browser uploads/downloads.

    ``origins`` is the app's configured origin allowlist (``CORS_ORIGINS``); with
    none configured there is nothing to allow, so this is a no-op. ``POST``/``PUT``
    cover the presigned-upload POST, ``GET``/``HEAD`` cover presigned downloads and
    ``<audio>`` playback; ``ETag`` is exposed so the client can read it back.
    """
    if not origins:
        logger.warning(
            "No CORS_ORIGINS configured; skipping bucket CORS on %s "
            "(browser uploads will be blocked by real S3)",
            bucket,
        )
        return

    client = get_storage_client()
    try:
        client.put_bucket_cors(
            Bucket=bucket,
            CORSConfiguration={
                "CORSRules": [
                    {
                        "AllowedMethods": ["POST", "PUT", "GET", "HEAD"],
                        "AllowedOrigins": origins,
                        "AllowedHeaders": ["*"],
                        "ExposeHeaders": ["ETag"],
                        "MaxAgeSeconds": 3600,
                    }
                ]
            },
        )
    except ClientError:  # pragma: no cover - not all S3 clones implement this
        logger.warning("Could not set CORS policy on %s", bucket)


def upload_file(bucket: str, path: str, data: bytes, content_type: str = "audio/wav") -> str:
    """Upload bytes to a bucket. Returns the storage key."""
    client = get_storage_client()
    client.put_object(Bucket=bucket, Key=path, Body=data, ContentType=content_type)
    return path


def get_signed_url(bucket: str, path: str, expires_in: int = 3600) -> str:
    """Generate a short-lived signed download URL."""
    client = get_storage_client()
    return client.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": path},
        ExpiresIn=expires_in,
    )


def download_file(bucket: str, path: str) -> bytes:
    """Download an object's bytes (server-side, via the app's IAM credentials).

    Used by the worker to pull a project's input audio before running the engine.
    """
    client = get_storage_client()
    response = client.get_object(Bucket=bucket, Key=path)
    return response["Body"].read()


def create_signed_upload_url(
    bucket: str,
    path: str,
    *,
    content_type: str,
    max_size_bytes: int,
    expires_in: int = 3600,
) -> dict[str, Any]:
    """Mint a presigned POST the client can upload *directly* to.

    Returns ``{"url": ..., "fields": {...}, "path": ...}``. The file bytes never
    pass through the API and the AWS credentials stay server-side.

    This is a presigned **POST**, not a PUT, specifically so the size and content
    type stay enforced by storage itself: the policy carries a
    ``content-length-range`` and an exact ``Content-Type`` condition, and S3
    rejects a violating upload outright. (Supabase enforced the equivalent via the
    bucket's own ``file_size_limit`` / ``allowed_mime_types``, which S3 has no
    counterpart for — a presigned PUT would silently accept a file of any size.)

    The client must send ``multipart/form-data`` to ``url`` with every entry of
    ``fields`` included before the ``file`` part.
    """
    client = get_storage_client()
    response = client.generate_presigned_post(
        Bucket=bucket,
        Key=path,
        Fields={"Content-Type": content_type},
        Conditions=[
            {"Content-Type": content_type},
            ["content-length-range", 1, max_size_bytes],
        ],
        ExpiresIn=expires_in,
    )
    return {"url": response["url"], "fields": response["fields"], "path": path}


def _list_keys(bucket: str, prefix: str) -> list[str]:
    """Full object keys stored directly under ``prefix`` (non-recursive)."""
    client = get_storage_client()
    normalized = prefix.rstrip("/") + "/"
    keys: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=normalized, Delimiter="/"):
        for obj in page.get("Contents", []):
            # Skip the folder placeholder object some tools create for a prefix.
            if obj["Key"] != normalized:
                keys.append(obj["Key"])
    return keys


def list_objects(bucket: str, prefix: str) -> list[dict]:
    """List objects stored directly under ``prefix`` (non-recursive).

    Returns ``[{"name": <basename>, "metadata": {"size": int, "mimetype": str}}]``.
    ``list_objects_v2`` reports size but not content type, so each object is
    HEADed for it — fine here, where a prefix holds one project's handful of
    input files.
    """
    client = get_storage_client()
    normalized = prefix.rstrip("/") + "/"
    objects: list[dict] = []
    for key in _list_keys(bucket, prefix):
        head = client.head_object(Bucket=bucket, Key=key)
        objects.append(
            {
                "name": key[len(normalized) :],
                "metadata": {
                    "size": int(head.get("ContentLength") or 0),
                    "mimetype": head.get("ContentType") or "application/octet-stream",
                },
            }
        )
    return objects


def delete_file(bucket: str, path: str) -> None:
    client = get_storage_client()
    client.delete_object(Bucket=bucket, Key=path)


def delete_prefix(bucket: str, prefix: str) -> None:
    """Delete every object stored directly under ``prefix`` (non-recursive).

    Used to purge a project's storage when the project (or all its uploads) is
    deleted. No-op when the prefix is already empty.
    """
    client = get_storage_client()
    keys = _list_keys(bucket, prefix)
    # delete_objects caps at 1000 keys per call.
    for batch_start in range(0, len(keys), 1000):
        batch = keys[batch_start : batch_start + 1000]
        client.delete_objects(
            Bucket=bucket,
            Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
        )
