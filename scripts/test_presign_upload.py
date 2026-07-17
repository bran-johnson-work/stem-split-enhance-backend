"""End-to-end smoke test for the presigned-URL upload flow, against a live server.

Walks the full happy path a real client follows:

    login -> POST /projects -> POST /projects/{id}/files/upload-url
          -> POST bytes straight to S3 -> POST /projects/{id}/upload-complete
          -> GET /projects/{id} + GET /projects/{id}/input-url

Unlike the pytest suite (which is network-free and fully mocked), this makes real
HTTP calls to a running API *and* to S3, so it needs the app running and working
AWS credentials (or LocalStack up). Use it to sanity-check the flow by hand.

    # start LocalStack + the API first:
    #   docker run --rm -p 4566:4566 localstack/localstack
    #   python -m scripts.init_storage
    #   python -m app.main
    python -m scripts.test_presign_upload ~/Desktop/PoC/audio1_stems/vocals.wav \
        --email tester@example.com --password 'Passw0rd!' --register

Exit code is 0 on success, non-zero on the first failed step.
"""

import argparse
import mimetypes
from pathlib import Path

import httpx

from app.core.config import get_settings

# Map common audio extensions to the content types the API accepts, so the
# caller usually doesn't have to pass --content-type by hand.
_CONTENT_TYPES = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
}


def _guess_content_type(path: Path, override: str | None) -> str:
    if override:
        return override.lower()
    ext = path.suffix.lower()
    if ext in _CONTENT_TYPES:
        return _CONTENT_TYPES[ext]
    guessed, _ = mimetypes.guess_type(str(path))
    return (guessed or "application/octet-stream").lower()


def _login(client: httpx.Client, base: str, email: str, password: str) -> str:
    # OAuth2 password grant -> form-encoded, not JSON.
    resp = client.post(
        f"{base}/api/v1/auth/login",
        data={"username": email, "password": password},
    )
    if resp.status_code != 200:
        raise RuntimeError(f"login failed ({resp.status_code}): {resp.text}")
    token = resp.json()["access_token"]
    print(f"[ok] logged in as {email}")
    return token


def _register(client: httpx.Client, base: str, email: str, password: str) -> None:
    resp = client.post(
        f"{base}/api/v1/auth/register",
        json={"email": email, "password": password, "full_name": "Presign Tester"},
    )
    if resp.status_code == 201:
        print(f"[ok] registered {email}")
    elif resp.status_code == 400:
        # Already exists (or otherwise unregisterable) — fine, we'll just log in.
        print(f"[info] register skipped ({email} likely already exists)")
    else:
        raise RuntimeError(f"register failed ({resp.status_code}): {resp.text}")


def run(args: argparse.Namespace) -> int:
    file_path = Path(args.file).expanduser()
    if not file_path.is_file():
        print(f"[error] file not found: {file_path}")
        return 1

    data = file_path.read_bytes()
    if not data:
        print(f"[error] file is empty: {file_path}")
        return 1

    content_type = _guess_content_type(file_path, args.content_type)
    base = args.base_url.rstrip("/")
    settings = get_settings()
    endpoint = settings.aws_endpoint_url or f"AWS S3 ({settings.aws_region})"
    project_name = args.name or file_path.stem

    print(f"[info] file={file_path.name} ({len(data)} bytes) content_type={content_type}")
    print(f"[info] api={base}  s3={endpoint}")

    auth: dict[str, str]
    with httpx.Client(timeout=60.0) as client:
        # 1) auth ----------------------------------------------------------
        if args.register:
            _register(client, base, args.email, args.password)
        token = _login(client, base, args.email, args.password)
        auth = {"Authorization": f"Bearer {token}"}

        # 2) create an empty project --------------------------------------
        resp = client.post(
            f"{base}/api/v1/projects", json={"name": project_name}, headers=auth
        )
        if resp.status_code != 201:
            print(f"[error] create project failed ({resp.status_code}): {resp.text}")
            return 1
        project = resp.json()
        project_id = project["id"]
        print(f"[ok] created project {project_id} status={project['status']}")

        # 3) mint a presigned upload URL for a file in that project -------
        resp = client.post(
            f"{base}/api/v1/projects/{project_id}/files/upload-url",
            json={"filename": file_path.name, "content_type": content_type},
            headers=auth,
        )
        if resp.status_code != 201:
            print(f"[error] presign failed ({resp.status_code}): {resp.text}")
            return 1
        signed = resp.json()
        print(f"[ok] got presigned POST for key={signed['storage_key']}")

        # 4) upload the bytes DIRECTLY to S3 (not through the API) --------
        # A presigned POST is multipart/form-data: every signed field first, then
        # the file part last. S3 enforces the policy's size/content-type here.
        upload = client.post(
            signed["url"],
            data=signed["fields"],
            files={"file": (file_path.name, data, content_type)},
        )
        if upload.status_code not in (200, 201, 204):
            print(f"[error] storage POST failed ({upload.status_code}): {upload.text}")
            return 1
        print(f"[ok] uploaded bytes to storage ({upload.status_code})")

        # 5) finalise: the API verifies the object landed and queues it ---
        resp = client.post(
            f"{base}/api/v1/projects/{project_id}/upload-complete", headers=auth
        )
        if resp.status_code != 200:
            print(f"[error] upload-complete failed ({resp.status_code}): {resp.text}")
            return 1
        finalised = resp.json()
        files = finalised.get("input_files", [])
        print(
            f"[ok] finalised: status={finalised['status']} "
            f"input_files={len(files)}"
        )

        # 6) verify: fetch the project and a signed download URL ----------
        resp = client.get(f"{base}/api/v1/projects/{project_id}", headers=auth)
        if resp.status_code != 200:
            print(f"[error] read project failed ({resp.status_code}): {resp.text}")
            return 1

        resp = client.get(
            f"{base}/api/v1/projects/{project_id}/input-url", headers=auth
        )
        if resp.status_code != 200:
            print(f"[error] input-url failed ({resp.status_code}): {resp.text}")
            return 1
        print(f"[ok] download URL: {resp.json()['url']}")

    print("\n[done] presigned upload flow succeeded ✅")
    print(f"       project_id={project_id}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Smoke-test the presigned-URL upload flow against a live server."
    )
    parser.add_argument("file", help="path to an audio file to upload")
    parser.add_argument(
        "--base-url", default="http://localhost:8000", help="API base URL"
    )
    parser.add_argument("--email", default="tester@example.com")
    parser.add_argument("--password", default="Passw0rd!")
    parser.add_argument(
        "--register", action="store_true", help="register the user before logging in"
    )
    parser.add_argument("--name", help="project name (defaults to the file stem)")
    parser.add_argument(
        "--content-type", help="override the declared content type (else auto-detected)"
    )
    args = parser.parse_args()

    try:
        raise SystemExit(run(args))
    except KeyboardInterrupt:
        print("\n[aborted]")
        raise SystemExit(130)
    except (httpx.HTTPError, RuntimeError) as exc:
        print(f"[error] {exc}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
