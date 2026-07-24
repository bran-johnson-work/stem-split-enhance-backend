"""S3 storage-layer tests, run against moto's in-process S3 API implementation.

The rest of the suite mocks ``app.core.storage`` wholesale, which would leave the
boto3 calls themselves untested. These exercise the real calls — still with no
network and no AWS account — so the shapes the services depend on (notably
``list_objects``' Supabase-compatible dict and the presign policy's conditions)
are pinned.
"""

import base64
import json

import pytest

from app.core import storage

BUCKET = "test-bucket"
PREFIX = "user-1/proj-1"


@pytest.fixture
def s3(monkeypatch):
    """A live (in-process) S3 with an empty bucket, wired into app.core.storage."""
    moto = pytest.importorskip("moto")
    settings = storage.get_settings()
    monkeypatch.setattr(settings, "aws_endpoint_url", "", raising=False)
    monkeypatch.setattr(settings, "aws_region", "us-east-1", raising=False)
    monkeypatch.setattr(settings, "aws_access_key_id", "test", raising=False)
    monkeypatch.setattr(settings, "aws_secret_access_key", "test", raising=False)

    with moto.mock_aws():
        storage.get_storage_client.cache_clear()
        storage.ensure_bucket(BUCKET, public=False)
        yield
    storage.get_storage_client.cache_clear()


def test_ensure_bucket_is_idempotent(s3):
    # The fixture already created it; a second call must not raise.
    storage.ensure_bucket(BUCKET, public=False)


def test_upload_download_round_trip(s3):
    key = f"{PREFIX}/song.wav"
    assert storage.upload_file(BUCKET, key, b"audio-bytes", "audio/wav") == key
    assert storage.download_file(BUCKET, key) == b"audio-bytes"


def test_list_objects_shape_matches_what_complete_upload_reads(s3):
    """ProjectService.complete_upload reads o["name"] and o["metadata"]["size"|"mimetype"]."""
    storage.upload_file(BUCKET, f"{PREFIX}/song.wav", b"x" * 99, "audio/mpeg")

    objects = storage.list_objects(BUCKET, PREFIX)

    assert objects == [
        {"name": "song.wav", "metadata": {"size": 99, "mimetype": "audio/mpeg"}}
    ]


def test_list_objects_is_not_recursive(s3):
    """Supabase's list only returned direct children; the S3 port must match."""
    storage.upload_file(BUCKET, f"{PREFIX}/song.wav", b"a", "audio/wav")
    storage.upload_file(BUCKET, f"{PREFIX}/stems/vocals.wav", b"b", "audio/wav")

    assert [o["name"] for o in storage.list_objects(BUCKET, PREFIX)] == ["song.wav"]


def test_get_signed_url_is_signed_and_expiring(s3):
    storage.upload_file(BUCKET, f"{PREFIX}/song.wav", b"a", "audio/wav")

    url = storage.get_signed_url(BUCKET, f"{PREFIX}/song.wav", expires_in=60)

    assert "X-Amz-Signature=" in url
    assert "X-Amz-Expires=60" in url


def test_presigned_post_policy_carries_size_and_type_conditions(s3):
    """S3 has no bucket-level cap, so these conditions are the only thing enforcing it."""
    key = f"{PREFIX}/song.wav"

    signed = storage.create_signed_upload_url(
        BUCKET, key, content_type="audio/wav", max_size_bytes=20 * 1024 * 1024
    )

    assert set(signed) == {"url", "fields", "path"}
    assert signed["path"] == key
    assert signed["fields"]["key"] == key
    assert signed["fields"]["Content-Type"] == "audio/wav"

    policy = json.loads(base64.b64decode(signed["fields"]["policy"]))
    assert ["content-length-range", 1, 20 * 1024 * 1024] in policy["conditions"]
    assert {"Content-Type": "audio/wav"} in policy["conditions"]


def test_ensure_bucket_sets_cors_for_browser_uploads(s3):
    """Real S3 blocks the browser's direct presigned POST without a bucket CORS policy."""
    client = storage.get_storage_client()

    rules = client.get_bucket_cors(Bucket=BUCKET)["CORSRules"]

    assert len(rules) == 1
    rule = rules[0]
    assert "POST" in rule["AllowedMethods"]
    assert "GET" in rule["AllowedMethods"]
    # Default settings.cors_origins is "*", so that is what gets allowed here.
    assert rule["AllowedOrigins"] == ["*"]


def test_put_bucket_cors_is_a_noop_without_origins(s3):
    from botocore.exceptions import ClientError

    client = storage.get_storage_client()
    client.delete_bucket_cors(Bucket=BUCKET)

    storage.put_bucket_cors(BUCKET, [])  # no origins configured

    with pytest.raises(ClientError):  # still no CORS config on the bucket
        client.get_bucket_cors(Bucket=BUCKET)


def test_delete_file(s3):
    storage.upload_file(BUCKET, f"{PREFIX}/song.wav", b"a", "audio/wav")

    storage.delete_file(BUCKET, f"{PREFIX}/song.wav")

    assert storage.list_objects(BUCKET, PREFIX) == []


def test_delete_prefix_purges_only_its_own_prefix(s3):
    storage.upload_file(BUCKET, f"{PREFIX}/one.wav", b"a", "audio/wav")
    storage.upload_file(BUCKET, f"{PREFIX}/two.wav", b"b", "audio/wav")
    storage.upload_file(BUCKET, "user-1/proj-2/other.wav", b"c", "audio/wav")

    storage.delete_prefix(BUCKET, PREFIX)

    assert storage.list_objects(BUCKET, PREFIX) == []
    # A sibling project sharing the user prefix must survive.
    assert [o["name"] for o in storage.list_objects(BUCKET, "user-1/proj-2")] == ["other.wav"]


def test_delete_prefix_on_empty_prefix_is_a_noop(s3):
    # delete_objects rejects an empty key list, so this must short-circuit.
    storage.delete_prefix(BUCKET, "user-1/nonexistent")
