# Fix design: only return stems the track actually contains

**Status:** implemented (`app/engine/silence.py`, wired into `app/engine/pipeline.py`)
**Original request:** *"tôi muốn bạn chỉ return những stems trong audio thật sự có, nếu không thì hãy bỏ qua."*

## The bug

The engine returned a fixed set of 9 stems for every input, regardless of content. A
solo-piano upload produced `drums.wav`, `kick.wav`, `snare.wav`, `hihats.wav`,
`bass.wav` and `guitar.wav` — six files a listener would call empty.

This is not a coding mistake in the old pipeline so much as a misunderstanding of what a
separation model returns. `htdemucs_ft` and `htdemucs_6s` always emit every source they
were trained on. Asked for guitar on a track with no guitar, they do not return silence —
they return a low-level residual made of bleed, reverb tails and model noise. The old
`separate_and_finalize` loop wrote every stem whose array existed:

```python
present = [n for n in FULL_STEM_ORDER if raw_stems.get(n) is not None]
```

`raw_stems[n]` is never `None`, so `present` was always the full catalogue.

## Detection design

The gate lives in `app/engine/silence.py` — numpy only, no torch or scipy, so it imports
and tests in the lean API environment as well as on the worker.

### Why two thresholds

Neither threshold works alone:

| Threshold | Catches | Fails when |
|---|---|---|
| Absolute peak floor (dBFS) | true silence, near-silence | the track is mastered quietly — the same performance 12 dB down starts failing |
| Relative to the mix (dB) | level-independent, context-aware | a residual that is loud-ish in absolute terms on a very quiet track |

A stem is kept only when it clears **both**:

```
peak_dbfs >= stem_silence_peak_dbfs            (default -50.0)
loudest_window_dbfs - mix_rms_dbfs >= stem_silence_relative_rms_db   (default -38.0)
```

### Why loudest-window RMS, not overall RMS

This is the decision that matters most. An instrument playing only in the bridge — say
8 bars of piano in a 4-minute track — has negligible *average* energy, so an overall-RMS
gate throws away real content. Measuring the loudest 1-second window
(`stem_silence_window_seconds`) keeps sparse-but-real stems and still rejects a residual
that is uniformly quiet everywhere.

Windows do not overlap, except that the final `window` samples are always evaluated as
one block, so content confined to a trailing partial window is not missed.

### Reference level

`mix_rms_dbfs` is the overall RMS of the decoded input mix, computed once in
`separate_all` from the same tensor fed to the model. Using the mix (rather than a
per-stem or cross-stem statistic) makes the relative floor mean something stable: *how
loud is this stem compared with the track it came from*.

## Behaviour

Wired into `separate_all`, so rejected stems are dropped **before** enhancement or
writing — the gate saves work rather than costing it.

1. Separate with `htdemucs_ft`, then `htdemucs_6s` for guitar/piano.
2. Gate all model outputs against the mix. Drop the failures.
3. If `drums` survived, band-split it and gate the three sub-stems **independently** — a
   kit with no cymbals yields a real kick and snare but an empty hi-hats band.
4. If `drums` was dropped, skip the split entirely. Its band-split products would be
   nothing but filtered noise, and skipping saves the DSP.

### Safeguards

- **All-empty fallback.** If the gate would reject *every* stem, nothing is dropped and a
  warning is logged. An all-empty verdict means the mix reference is degenerate — a
  silent or corrupt upload — so the measurement is not trustworthy enough to return an
  empty result. A job with zero outputs reads as a backend failure, not as an answer.
- **Audit trail.** Every stem gets a `StemVerdict` carrying the measurement and a
  human-readable reason, logged at INFO when kept and WARNING when dropped.
  *"Where did my drums go"* is the one support question this feature creates, and it has
  to be answerable from the worker log alone:
  ```
  silence.drop stem=guitar loudest -71.2 dBFS is -56.4 dB below the mix (floor -38.0)
  ```
- **Pin list.** `STEM_FILTER_ALWAYS_KEEP=vocals,drums` never drops those stems, whatever
  the measurement says — an escape hatch if a product contract promises a fixed set.
- **Kill switch.** `STEM_FILTER_ENABLED=false` restores the old fixed stem set exactly.

## Threshold calibration

Defaults are deliberately permissive. **Wrongly dropping a stem the user can hear is a
much worse failure than shipping one extra near-silent file**, so both floors sit well
below anything musically audible. Measured on the synthetic fixtures in
`tests/test_engine_silence.py`:

| Case | loudest window vs mix | Verdict at defaults |
|---|---|---|
| Lead vocal, present throughout | ~-4 dB | keep |
| Instrument present 0.5 s in 8 s | ~-6 dB | keep |
| Quiet accompaniment | ~-12 dB | keep |
| Model residual for an absent source | ~-56 dB | **drop** |
| Digital silence | floored | **drop** |

Tune with `STEM_SILENCE_RELATIVE_RMS_DB`: toward `-30` prunes more aggressively, toward
`-45` keeps more.

## Known limitation

An energy gate cannot fix a model that hallucinates content. `htdemucs_6s` leaks
non-guitar material into its guitar stem loudly enough that on some tracks with no guitar
the residual clears both floors and is kept. The gate removes the obviously-empty
stems, not the badly-separated ones — that is a model-quality problem, addressed by the
v2 work in [stem-quality-v2.md](stem-quality-v2.md), not by thresholding. Lowering the
floors far enough to catch it would start pruning real quiet instruments, which is the
worse trade.

## Config

```bash
STEM_FILTER_ENABLED=true            # false restores the old fixed 9-stem set
STEM_SILENCE_PEAK_DBFS=-50.0        # absolute floor
STEM_SILENCE_RELATIVE_RMS_DB=-38.0  # loudest window vs mix RMS
STEM_SILENCE_WINDOW_SECONDS=1.0     # relative-measurement window
STEM_FILTER_ALWAYS_KEEP=            # comma-separated stems never dropped
```

## API impact

None breaking. `GET /projects/{id}/jobs/{job_id}/stems` already returns whatever
`output_files` rows exist, and nothing in the codebase hard-codes a stem count —
`STEM_NAMES` in `app/models/output_file.py` is an allow-list, not a required set.
Clients that assumed 9 files must render the returned list instead. See the v2 UI plan
for how the comparison view handles a stem present in one version and absent in another.

## Tests

`tests/test_engine_silence.py` — 20 tests, numpy-only (no torch), covering the
measurement primitives (dBFS flooring, empty arrays, mono and stereo, sub-window and
trailing content), the keep/drop decision (real / silent / low-level residual / sparse /
level-independence / loud-residual-on-a-quiet-track), and selection over a full stem set
(pin list, all-empty fallback, threshold tunability).
