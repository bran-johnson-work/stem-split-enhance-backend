"""Download the v2 (Mel-Band RoFormer) weights and print their SHA-256.

Two uses:

1. **Pin the checkpoint.** Run once, copy the printed hash into
   ``V2_VOCALS_CKPT_SHA256``. After that a silent upstream re-publish becomes a hard
   failure at load time instead of an unexplained quality change weeks later.
2. **Bake weights into an image.** Add ``RUN python -m scripts.fetch_v2_weights`` to
   Dockerfile.worker.v2 so a scaling fleet doesn't re-download ~600 MB per cold start.

    python -m scripts.fetch_v2_weights
"""

import sys

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.engine import weights
from app.engine.roformer import CKPT_FILENAME, CONFIG_FILENAME

# Refuse to start a ~600 MB download onto a volume that cannot hold it — failing here
# with a clear message beats filling the disk and taking the worker down with it.
REQUIRED_FREE_BYTES = 2 * 1024 * 1024 * 1024


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level)
    cache = weights.weights_dir(settings.v2_weights_dir)

    free = weights.free_space_bytes(cache)
    if free < REQUIRED_FREE_BYTES:
        print(
            f"Only {free / 1e9:.1f} GB free at {cache} — need at least "
            f"{REQUIRED_FREE_BYTES / 1e9:.1f} GB. Mount a bigger volume or set "
            "V2_WEIGHTS_DIR.",
            file=sys.stderr,
        )
        return 1

    print(f"Cache: {cache}")
    config_path = weights.ensure_file(
        settings.v2_vocals_config_url, CONFIG_FILENAME, cache_dir=cache
    )
    ckpt_path = weights.ensure_file(
        settings.v2_vocals_ckpt_url,
        CKPT_FILENAME,
        cache_dir=cache,
        sha256=settings.v2_vocals_ckpt_sha256,
    )

    ckpt_sha = weights.sha256_file(ckpt_path)
    size_mb = ckpt_path.stat().st_size / 1e6
    print(f"\nconfig: {config_path}")
    print(f"ckpt:   {ckpt_path}  ({size_mb:.0f} MB)")
    print(f"\nsha256: {ckpt_sha}")
    if settings.v2_vocals_ckpt_sha256:
        print("Already pinned via V2_VOCALS_CKPT_SHA256 — verified above.")
    else:
        print("\nPin it so an upstream change fails loudly instead of silently:")
        print(f"  V2_VOCALS_CKPT_SHA256={ckpt_sha}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
