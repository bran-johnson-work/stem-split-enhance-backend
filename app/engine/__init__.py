"""Stem-separation engine (migrated from the POC).

Framework-free audio compute: HT-Demucs separation + a DSP enhancement chain.
This package must have NO dependency on FastAPI, the DB, or storage — the worker
(``app.worker``) drives it. It is intentionally *not* imported anywhere in the API
process, so the heavy torch/demucs stack is only installed on the worker image.

Public surface:
    - ``separate_and_finalize`` — the whole pipeline for one input file.
    - ``FULL_STEM_ORDER`` / ``ENHANCE_STEMS`` — the stem catalogue.
    - ``SEPARATORS`` — engine versions this build can run (v1 = HT-Demucs,
      v2 = Mel-Band RoFormer vocals + HT-Demucs rhythm section).
    - ``engine_config_snapshot`` — the settings a run used, for the job record.

``app.engine.roformer`` is intentionally NOT imported here: it needs ``bs-roformer``,
which only the v2 worker image installs, so importing it eagerly would break the v1
image. The v2 code path imports it lazily.
"""

from app.engine.pipeline import (
    ENHANCE_STEMS,
    FULL_STEM_ORDER,
    SEPARATORS,
    FinalizedStem,
    engine_config_snapshot,
    finalize_stem,
    separate_all,
    separate_and_finalize,
)

__all__ = [
    "ENHANCE_STEMS",
    "FULL_STEM_ORDER",
    "SEPARATORS",
    "FinalizedStem",
    "engine_config_snapshot",
    "finalize_stem",
    "separate_all",
    "separate_and_finalize",
]
