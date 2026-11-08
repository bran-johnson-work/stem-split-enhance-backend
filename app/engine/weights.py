"""Fetch and cache v2 model weights.

The v1 stack gets its weights through ``torch.hub`` (demucs knows its own URLs). The v2
checkpoints come from Hugging Face and a GitHub raw config, so we fetch them ourselves.

Two things this module exists to guarantee:

* **Integrity.** A community checkpoint that changes under you is a quality regression
  with no diff to review and no way to bisect. Downloads are SHA-256 verified when a hash
  is pinned, and loudly warned about when one is not.
* **Idempotence.** Many worker processes on one host share a weights volume, and they all
  start at once after a deploy. Downloads go to a unique temp file and are atomically
  renamed into place, so concurrent starts cannot read a half-written checkpoint.

Deliberately stdlib-only (``urllib``) — no new dependency to fetch a dependency.
"""

import hashlib
import logging
import os
import shutil
import tempfile
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

# Read in 8 MB blocks: these files are 100 MB - 1 GB, and the hash is computed on the
# same pass as the write so a big checkpoint is never held in memory.
_CHUNK_BYTES = 8 * 1024 * 1024
_USER_AGENT = "klearnote-stem-worker/1.0"


def weights_dir(configured: str = "") -> Path:
    """Directory v2 checkpoints are cached in.

    Defaults under ``TORCH_HOME`` so the ``/cache/torch`` volume the worker image already
    mounts persists them next to the demucs weights — one volume to provision, not two.
    """
    if configured:
        base = Path(configured)
    else:
        torch_home = os.environ.get("TORCH_HOME")
        base = Path(torch_home) / "v2" if torch_home else Path.home() / ".cache" / "stem-v2"
    base.mkdir(parents=True, exist_ok=True)
    return base


def sha256_file(path: Path) -> str:
    """Streamed SHA-256 of a file on disk."""
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def _download_to(url: str, dest: Path) -> str:
    """Download ``url`` to ``dest`` atomically. Returns the SHA-256 of what was written.

    Writes to a temp file in the destination directory (same filesystem, so the rename is
    atomic) and hashes during the same pass.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    fd, tmp_name = tempfile.mkstemp(dir=str(dest.parent), prefix=f".{dest.name}.", suffix=".part")
    tmp = Path(tmp_name)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
        with urllib.request.urlopen(request) as response, os.fdopen(fd, "wb") as out:
            while chunk := response.read(_CHUNK_BYTES):
                digest.update(chunk)
                out.write(chunk)
        # os.replace is atomic on POSIX and Windows, and overwrites an existing file, so a
        # worker that lost the race just gets the other worker's identical result.
        os.replace(tmp, dest)
        return digest.hexdigest()
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def ensure_file(url: str, filename: str, *, cache_dir: Path, sha256: str = "") -> Path:
    """Return a local path to ``url``, downloading it once and verifying it if pinned.

    A cached file whose hash does not match a pinned ``sha256`` is treated as corrupt and
    re-downloaded once; if the fresh download also mismatches, that is a real integrity
    failure (wrong URL, tampered mirror, upstream re-publish) and raises rather than
    feeding unknown weights into production.
    """
    dest = cache_dir / filename
    expected = (sha256 or "").strip().lower()

    if dest.exists():
        if not expected:
            logger.warning(
                "weights.unpinned name=%s — no SHA-256 configured, integrity NOT verified. "
                "Run `python -m scripts.fetch_v2_weights` and pin V2_VOCALS_CKPT_SHA256.",
                filename,
            )
            return dest
        actual = sha256_file(dest)
        if actual == expected:
            logger.info("weights.cached name=%s sha256=%s", filename, actual[:12])
            return dest
        logger.warning(
            "weights.corrupt name=%s expected=%s actual=%s — re-downloading",
            filename, expected[:12], actual[:12],
        )
        dest.unlink(missing_ok=True)

    logger.info("weights.download name=%s url=%s", filename, url)
    actual = _download_to(url, dest)

    if expected and actual != expected:
        dest.unlink(missing_ok=True)
        raise RuntimeError(
            f"Checksum mismatch for {filename}: expected {expected}, got {actual}. "
            f"The file at {url} is not the pinned checkpoint — refusing to load it."
        )
    if not expected:
        logger.warning(
            "weights.unpinned name=%s sha256=%s — pin this as V2_VOCALS_CKPT_SHA256 "
            "so a silent upstream change becomes a hard failure instead of a "
            "quality regression.",
            filename, actual,
        )
    else:
        logger.info("weights.verified name=%s sha256=%s", filename, actual[:12])
    return dest


def free_space_bytes(path: Path) -> int:
    """Free bytes on the filesystem holding ``path`` (for a preflight check)."""
    return shutil.disk_usage(str(path)).free
