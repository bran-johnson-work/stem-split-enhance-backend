"""Stem-separation engine (migrated from the POC).

Framework-free audio compute: HT-Demucs separation + a DSP enhancement chain.
This package must have NO dependency on FastAPI, the DB, or storage — the worker
(``app.worker``) drives it. It is intentionally *not* imported anywhere in the API
process, so the heavy torch/demucs stack is only installed on the worker image.

Public surface:
    - ``separate_and_finalize`` — the whole pipeline for one input file.
    - ``FULL_STEM_ORDER`` / ``ENHANCE_STEMS`` — the stem catalogue.
"""

from app.engine.pipeline import (
    ENHANCE_STEMS,
    FULL_STEM_ORDER,
    FinalizedStem,
    finalize_stem,
    separate_all,
    separate_and_finalize,
)

__all__ = [
    "ENHANCE_STEMS",
    "FULL_STEM_ORDER",
    "FinalizedStem",
    "finalize_stem",
    "separate_all",
    "separate_and_finalize",
]
