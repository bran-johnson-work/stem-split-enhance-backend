# Stem quality: v1 audit, v2 model research, and migration plan

Research current as of **2026-08-03**. Leaderboard numbers move weekly — re-check before
committing spend.

---

## 1. Audit of what is deployed today (v1)

### Models

| Role | Model | Version | Weights source |
|---|---|---|---|
| vocals / drums / bass / other | **`htdemucs_ft`** (bag of 4) | demucs 4.0.1 (`Settings.demucs_model`) | `torch.hub` → `dl.fbaipublicfiles.com/demucs/hybrid_transformer/` |
| guitar / piano | **`htdemucs_6s`** | demucs 4.0.1 (`Settings.instruments_model`) | same |
| kick / snare / hihats | no model — Butterworth band-split | `app/engine/dsp.py::split_drums` | n/a |

~400 MB of weights, cached under `TORCH_HOME` on first run (`dsp.WEIGHTS_MB`).
`htdemucs_ft` is loaded through the demucs low-level API (`get_model` + `apply_model`);
4.0.1 on PyPI ships no `demucs.api`, so `separate_sources` re-implements the bag-of-models
weighted average itself in order to emit per-model progress.

### Inference pipeline

`app/engine/pipeline.py::separate_and_finalize`:

```
decode (soundfile, ffmpeg fallback for m4a)
  → htdemucs_ft bag: 4 sub-models, weighted-average  [shifts=1, overlap=0.5, split=True]
  → htdemucs_6s: take guitar + piano only
  → NEW: content gate — drop stems the track doesn't contain
  → band-split drums → kick / snare / hihats
  → per-stem enhancement chain (level = "full")
  → 24-bit PCM WAV per stem
```

Enhancement chain at `enhancement_level="full"` (`dsp.py::enhance_stem`):
noisereduce (`prop_decrease` 0.72 vocals / 0.45 others) → `_declick` → optional
reference-guided spectral match → `_harmonic_exciter` (0.32 vocals) → `_multiband_saturation`
→ safety limit.

### Output stem list

`vocals, drums, kick, snare, hihats, bass, guitar, piano, other` — 9, formerly fixed for
every input. The empty-stem gate ([empty-stem-filter.md](empty-stem-filter.md)) now makes
this per-track.

### Where quality falls short

Ordered by how much each costs us, with the evidence:

1. **Test-time augmentation is off.** `demucs_shifts: int = 1`, i.e. one pass, no
   averaging. The setting's own comment in `config.py` calls shifts *"the single biggest
   lever for reducing bleed between stems"*. We are running the POC value in production.
2. **The base model is ~3–4 dB of SDR behind the field.** `htdemucs_ft` scores ~8.2–8.9 dB
   vocals SDR; the current leaders are 11.9–12.3 dB. Roughly 3 dB is a halving of
   distortion energy, so this is the dominant term in "muddy vocals" and inter-stem bleed —
   not something post-processing recovers.
3. **The enhancement chain is tuned for Suno artifacts and runs on everything.** At
   `"full"` it applies aggressive spectral denoising, a harmonic exciter and tanh
   multiband saturation to real recordings. `config.py` already warns it *"can over-color
   clean real recordings"*. On real music this is very likely a **cause** of the reported
   artifacts, not a cure — it is additive harmonic distortion layered on top of separation
   artifacts.
4. **`_declick` is a crude gate.** It ducks samples to 0.15× wherever
   `|diff| > 0.07`, which chews sibilance and percussive transients.
5. **`_multiband_saturation` uses `sosfilt`, not `sosfiltfilt`** — phase distortion across
   three summed bands, smearing transients.
6. **kick/snare/hihats are cosmetic, not separated.** Overlapping Butterworth bands, which
   `dsp.py` documents as *"perceptual splits, not a lossless decomposition"*. Any non-drum
   content in those bands lands in them too.
7. **guitar/piano come from the weaker 6-source model.** `htdemucs_6s` scores below
   `htdemucs_ft` per stem and leaks "other" content into guitar and piano. This is the
   residual the empty-stem gate cannot reliably catch.
8. **Per-channel independent processing.** `enhance_stem` recurses per channel, so
   denoise and de-click decisions differ left vs right and can decorrelate the stereo
   image.

**Items 1, 3, 4 and 5 are config or code changes needing no new weights.** They should
ship before any model migration — see Phase 0.

---

## 2. Benchmark research

### MVSep Multisong leaderboard (SDR, dB, higher is better)

From [the vocals-sorted leaderboard](https://mvsep.com/quality_checker/multisong_leaderboard?sort=vocals),
fetched 2026-08-03. The top four rows are dataset reference entries (unprocessed original
stems and codec round-trips), not models; real models start below them.

| Model | Vocals | Instrum | Bass | Drums | Other | Weights |
|---|---|---|---|---|---|---|
| BS Roformer 124 bands (2026.07.10, MVSep) | **12.33** | **18.64** | — | — | — | hosted only |
| BS RoFormer 124 bands (High Fullness) | 12.31 | 18.52 | — | — | — | hosted only |
| BS PolarFormer 124 bands (2026.06.13, MVSep) | 12.02 | 18.33 | — | — | — | hosted only |
| **MVSep Ensemble (vocals, instrum) (2025.06.28)** | **11.93** | **18.24** | — | — | — | hosted only |
| MVSep Ensemble (5-stem) (2025.06.30) | 11.93 | 18.24 | 14.85 | 14.33 | 9.01 | hosted only |
| BS Roformer (2025.07.20, MVSep) | 11.89 | 18.20 | — | — | — | hosted only |
| sami-bytedance-v.1.1 | 11.82 | 18.13 | — | — | — | closed |
| unwa leap Xe | 11.76 | 17.53 | — | — | — | community |
| Deux + SCNet (Regular) | 11.63 | 17.85 | — | — | — | community |

### Open-weight singles ([ZFTurbo MSS-Training](https://github.com/ZFTurbo/Music-Source-Separation-Training/blob/main/docs/pretrained_models.md))

| Model | SDR | Notes |
|---|---|---|
| **SCNet XL IHF** | 10.089 MUSDB avg / **9.926 Multisong avg** | [release v1.0.15](https://github.com/ZFTurbo/Music-Source-Separation-Training/releases/tag/v1.0.15); *"training only on MUSDB18 training data"*; config change targets high frequencies |
| SCNet XL | 9.805 MUSDB avg | release v1.0.13 |
| Mel-Band RoFormer (KimberleyJensen) | 10.98 vocals | [HF weights](https://huggingface.co/KimberleyJSN/melbandroformer), MIT |
| BS Roformer (ZFTurbo) | 9.657 MUSDB avg | release v1.0.12 |
| **HTDemucs4 — our baseline** | **9.16 avg, 8.24 vocals** | `dl.fbaipublicfiles.com` |

*"IHF"* is not defined in the release notes. A DeepWiki page glosses it as "Intermediate
High Frequency"; that is AI-generated and unverified. The release notes themselves only
say *"Main changes in config is related to high frequencies."*

### Three findings that change the recommendation

**(a) The 2025.06 ensemble is no longer the frontier.** It sits ~4th among real models,
14 months old. A *single* BS Roformer 124-bands (2026.07) scores **+0.40 dB vocals** over
it at roughly a quarter of the compute. Adopting the 2025.06 ensemble as specified would
mean paying 4 model passes for less quality than one newer pass.

**(b) The ensemble is not a downloadable artifact.** "Ensemble (2025.06) — BS-RoFormer
(×2) + MelBand RoFormer (ft) + SCNet-XL (IHF)" is an **MVSep-hosted service
configuration**. The component architectures have open weights, but the exact recipe —
which two BS-RoFormer checkpoints, which MelBand fine-tune, the per-model blend weights —
is MVSep's. Reproducing it from open weights lands somewhere near 11.5–11.9 dB after
tuning, not exactly 11.93, and the tuning is real work. Every model at 11.9 dB and above
on that leaderboard is hosted-only or closed.

**(c) The ensemble does not produce our stem set.** It outputs **vocals + instrumental
only**. The 5-stem variant gets bass/drums/other from *Demucs* — the model we already run.
Guitar, piano and drum sub-parts come from MVSep's separate "Ensemble All-In", which their
own page calls *"quite slow but provides the most precise results."* So v2 is not a
drop-in replacement for 9 stems: it transforms the vocals/instrumental axis and leaves
bass/drums/other close to where they are.

### Licensing

| Component | Code | Weights | Training data |
|---|---|---|---|
| demucs / htdemucs_ft (current) | MIT | MIT (no separate statement) | *"MusDB + 800 songs"* |
| ZFTurbo MSS-Training + release ckpts | **MIT** (Roman Solovyev) | MIT — shipped from the MIT repo | varies |
| SCNet XL IHF | MIT | MIT | **MUSDB18 only** |
| Mel-Band RoFormer (KJ) | — | **MIT** | undisclosed (README empty) |

**The open question is the dataset, not the weights.** [MUSDB18](https://sigsep.github.io/datasets/musdb.html)
is academic-use: 46 tracks from MedleyDB under CC BY-NC-SA 4.0, 2 more under BY-NC-SA 3.0,
distributed on Zenodo with access requests. Whether a *model trained on* NC-licensed audio
inherits the restriction is legally unsettled and jurisdiction-dependent — I am not
qualified to resolve it and neither is a search result. **Route to counsel before a paid
v2 tier launches.**

Two mitigating facts: our current `htdemucs_ft` was trained on *"MusDB + 800 songs"*, so
this exposure already exists in production — v2 does not create it. And SCNet-XL IHF is
the *most* exposed candidate (MUSDB18-only), so a counsel "no" points toward Mel-Band
RoFormer, whose training data is undisclosed and separately licensed MIT.

### Cost and hardware

| Model | 3-min track, A40 | Notes |
|---|---|---|
| htdemucs (default) | 30–45 s | |
| htdemucs_6s | 40–60 s | |
| **htdemucs_ft (ours)** | **90–150 s** | bag of 4; ~$0.045/song on Replicate A40 |
| BS-RoFormer | 60–120 s | |

Source: [aistemsplitter.org benchmark, 2026-04-28](https://aistemsplitter.org/blog/htdemucs-vs-bs-roformer-vs-spleeter-2026-benchmark) —
a secondary vendor-blog source. Treat as order-of-magnitude, not as measurement.

**VRAM is not reliably documented for inference anywhere I could find.** The "40 GB GPU"
figure circulating for Mel-Band RoFormer is from KimberleyJensen's README and refers to
**training**, not inference — I checked the source. The ZFTurbo repo's "single NVIDIA A6000
48 GB" note is likewise about `training.batch_size`. Inference at the standard
`chunk_size=352800` (8 s at 44.1 kHz) is expected to fit comfortably in 24 GB and probably
in 12 GB, but **that is an estimate and Phase 0 must measure it.** Do not size instances
off this paragraph.

Compute accounting for a full v2 ensemble pipeline: 2× BS-RoFormer + 1× MelBand + 1×
SCNet-XL (vocals/instrumental) + 4× Demucs bag (bass/drums/other) + 1× 6s (guitar/piano)
= **9 model passes**, versus 5 today, with the RoFormer passes individually heavier.
Roughly **2–2.5× current cost per track.**

---

## 3. Recommendation

**Do not ship the 2025.06 ensemble as specified.** It is not downloadable, it is no longer
the frontier, it does not produce our stem set, and it costs 4 passes for the
vocals/instrumental axis alone.

Recommended sequence, in quality-per-dollar order:

### Phase 0 — free wins, no new weights (do this first)

| Change | Expected effect | Cost |
|---|---|---|
| `DEMUCS_SHIFTS=2` | measurably less inter-stem bleed | ~2× separation time |
| `ENHANCEMENT_LEVEL=light` | directly targets "muddy / over-processed" on real music | none (cheaper) |
| Replace `_declick` gate, or gate it to `full` only | stops transient chewing | none |
| `sosfilt` → `sosfiltfilt` in `_multiband_saturation` | removes phase smearing | negligible |

This is the honest first move: several of the reported quality complaints are plausibly
*our post-processing*, not the model. Establish the baseline with these on before
attributing anything to `htdemucs_ft`.

### v2a — recommended primary: one strong vocal model + keep Demucs for the rest

- **Mel-Band RoFormer (KJ, MIT, 10.98 vocals SDR)** or **SCNet-XL IHF (9.93 Multisong
  avg)** for vocals + instrumental.
- Keep `htdemucs_ft` for bass / drums / other and `htdemucs_6s` for guitar / piano.
- **~+2 to +2.7 dB vocals SDR** over baseline, **one extra model pass**, open MIT weights,
  no ensemble blend to tune.
- Captures most of the achievable gain for the least cost and risk. **This is the
  recommendation.**

### v2b — quality ceiling, later and optional

The 4-model ensemble. **~+0.9 dB over v2a for ~4× the vocal-path compute.** Clear
diminishing returns. Justified only as a paid "maximum quality" tier where the user is
explicitly buying render time, not as the default path.

### Alternative worth pricing: MVSep API

The genuinely-best models (BS Roformer 124 bands, 12.33 dB) are hosted-only — no amount of
self-hosting reaches them. Calling MVSep's API buys frontier quality with no GPU fleet.
Tradeoffs: per-track cost at scale, customer audio leaves our infrastructure (a privacy
commitment and probably a ToS/DPA question), and a hard vendor dependency on a
single-maintainer service. Worth a spike to price against a GPU fleet, especially at low
volume where a rented GPU idles.

### Summary of tradeoffs

| Option | Vocals SDR | Passes | Weights | Verdict |
|---|---|---|---|---|
| v1 today | ~8.2–8.9 | 5 | MIT | baseline |
| v1 + Phase 0 | ~8.5–9.2 | 9 | MIT | do first, free |
| **v2a** | **~11.0** | **6** | **MIT** | **recommended** |
| v2b ensemble | ~11.5–11.9 | 9 | MIT (SCNet: MUSDB18-only) | premium tier only |
| MVSep API | 12.33 | 0 (remote) | n/a | price it; privacy + vendor risk |

---

## 4. Implementation plan

### 4.1 Weights sourcing

Add a weights-fetch step rather than relying on `torch.hub`, since the new checkpoints come
from GitHub releases and Hugging Face:

- Pin exact URLs **and SHA-256** per model in a manifest (`app/engine/weights.py`). An
  unpinned community checkpoint silently changing is a quality regression with no diff.
- Bake into the worker image, or pull to a persistent volume on first boot (as
  `TORCH_HOME` works today). Baking makes cold starts predictable; a volume keeps the
  image small. With a GPU fleet that scales, **bake**.
- Keep v1 weights present in the same image — dual-run needs both.

### 4.2 Inference server changes

The layering already supports this; do not restructure it.

- New module `app/engine/roformer.py` alongside `dsp.py`, exposing the same shape as
  `separate_sources` → `{name: (channels, samples)}`. Model loading follows the existing
  `_models` dict + `_load_lock` pattern so pre-warm and job load stay idempotent.
- `app/engine/pipeline.py` gains a **version dispatch**: `separate_all_v1` (current) and
  `separate_all_v2`. `separate_and_finalize` takes an `engine_version` argument and routes.
  Everything downstream — the content gate, `finalize_stem`, WAV writing — is shared.
- The content gate is version-agnostic by construction: it takes arrays and a mix
  reference. No change needed.

### 4.3 Dependencies and runtime

- `requirements-worker.txt`: add `einops`, `rotary-embedding-torch`, `ml-collections`,
  `omegaconf` (RoFormer/SCNet deps). Do **not** vendor the whole ZFTurbo training repo —
  copy only the architecture modules needed for inference, with attribution.
- Only `Dockerfile.worker` changes. `Dockerfile.api` must stay torch-free — the COPY list
  there does not include `engine/` and must not start to.
- CUDA: keep the `TORCH_INDEX_URL` build arg. torch 2.11 ships cu126/cu128.

### 4.4 GPU sizing

- **Phase 0 deliverable: measure peak VRAM** for each candidate at `chunk_size=352800`
  and the chosen overlap, on one instance, before committing. The published numbers are
  training figures.
- Starting target: **24 GB — AWS `g5.xlarge` (A10G) or `g6.xlarge` (L4)**. Fall back to
  `chunk_size` reduction if measurement demands it; that costs quality at the seams, so
  prefer a bigger card.
- `WORKER_DEVICE=cuda` (not `auto`) on GPU hosts — `_pick_device` already fails fast
  rather than silently running 10–50× slower on CPU. Keep that.

### 4.5 A/B versioning system

**Schema.** Version belongs on the job — a job is one engine run.

```sql
ALTER TABLE processing_jobs
  ADD COLUMN engine_version varchar(16) NOT NULL DEFAULT 'v1',
  ADD COLUMN engine_config  jsonb;          -- shifts, overlap, model ids, thresholds
CREATE INDEX ix_processing_jobs_engine_version ON processing_jobs (engine_version);
```

`output_files` needs no new column — version is reachable through `job_id`. But the
**storage key must carry it**, or the two runs overwrite each other:

```
audio-stems/{user_id}/{project_id}/stems/{engine_version}/{stem}.wav
```

`engine_config` is what makes results interpretable six months later: a v2 stem rendered
at `shifts=1` is not the same artifact as one at `shifts=2`, and without the record you
cannot tell them apart.

**Dual-run.** With A/B on, `POST /process` creates **two** job rows, `v1` and `v2`.
Workers claim them independently through the existing `FOR UPDATE SKIP LOCKED` queue.
This gives three things for free:

- **Fallback is structural.** If the v2 job fails, the v1 job still delivers. No
  in-request retry logic, no half-written output set.
- Independent progress per version, which the UI needs anyway.
- Either job can be re-run alone.

Cost: two full renders per upload. Gate it hard (see rollout) and exempt it from plan
quota accounting — the user asked for one separation, not two.

**Comparison endpoint.**

```
GET /api/v1/projects/{id}/stems/compare
→ { versions: [ { engine_version, job_id, status, engine_config,
                  stems: [ { stem_name, is_enhanced, url, expires_in } ] } ] }
```

One call, both versions, signed URLs for each — the UI should not need to orchestrate two
job lookups to render a comparison.

**Settings.**

```bash
ENGINE_VERSION_DEFAULT=v1     # which version a normal single-run job uses
ENGINE_AB_ENABLED=false       # dual-run master switch
ENGINE_AB_PERCENT=0           # % of eligible projects dual-run
ENGINE_AB_USER_ALLOWLIST=     # user ids always dual-run (internal team)
ENGINE_V2_MODEL=mel_band_roformer
```

Bucketing must be **stable per project** (hash the project id), not random per request, or
a user's own project flips between versions on retry.

### 4.6 Rollout

| Stage | Gate | Watch | Rollback |
|---|---|---|---|
| 0 | Phase 0 config changes, all users | complaint rate, render time | revert env vars |
| 1 | `ENGINE_AB_USER_ALLOWLIST` = internal team | v2 crash rate, VRAM, wall-clock | none needed |
| 2 | `ENGINE_AB_PERCENT=5`, opt-in beta | blind-preference votes, GPU cost/track | set percent 0 |
| 3 | `ENGINE_AB_PERCENT=25` | same, plus queue depth | set percent 0 |
| 4 | `ENGINE_VERSION_DEFAULT=v2`, A/B off, v1 on demand | quality complaints, cost | flip default to v1 |

**Do not promote v2 on SDR alone.** SDR is a proxy; it disagrees with listeners often
enough to matter, and our enhancement chain sits downstream of it. Promote on Stage 2–3
blind-preference data — which is exactly what the UI below is for.

**Failure handling.** v2 job failure is already isolated by the dual-job design. Add: if
v2 fails while v1 succeeded, the compare endpoint returns v1 with the v2 entry carrying
`status: failed` — the UI shows a degraded state rather than an error. Once v2 is the
default (stage 4), a v2 failure should auto-enqueue a v1 job so the user still gets stems.

---

## 5. UI/UX plan: v1 vs v2 comparison

*(This repo is API-only. Below is the contract the frontend needs plus the interaction
design; implementation lands in the frontend repo.)*

### Default view

A segmented control on the project page: **Standard** / **Enhanced (beta)** / **Compare**.
Plain-language labels — "v1"/"v2" means nothing to a musician. Show a small
"how was this made" affordance that reveals `engine_config`.

### Compare mode

One row per stem. Both versions on the same row, sharing **one transport**.

The single most important requirement: **switching A↔B must be a gain crossfade on two
already-playing, sample-aligned sources — never a stop-and-reload.** If the switch
restarts playback or drops even 100 ms, users cannot hear the difference and the whole
feature is decorative. Both versions load, both play in sync, the toggle changes which one
is audible.

- Waveforms stacked, shared time axis, shared playhead and scrub.
- Per-stem solo/mute, and a level-matched toggle — v2 stems may sit at a different level,
  and *louder reads as better* to every listener. Loudness-match (per-stem LUFS) or the
  comparison is invalid.
- Download either or both; suffix filenames with the version.

### Blind mode

Hide the labels, randomise which side is A, collect a preference vote per stem. This is
the mechanism that decides promotion, so it needs a table:

```sql
CREATE TABLE stem_preferences (
  id uuid PRIMARY KEY,
  user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  project_id uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  stem_name varchar(50) NOT NULL,
  preferred_version varchar(16) NOT NULL,
  was_blind boolean NOT NULL DEFAULT true,
  created_at timestamptz NOT NULL DEFAULT now()
);
```

Record `was_blind` — unblinded votes are marketing data, blind votes are evidence, and
mixing them destroys both.

### Asymmetric and partial states

Two states the naive design gets wrong:

- **One version still rendering.** v2 takes ~2× as long. Show per-version status and let
  the user listen to v1 immediately; the compare toggle enables when the second lands.
  Never block the finished version behind the slow one.
- **Different stem sets between versions.** This is where the comparison feature and the
  empty-stem filter interact. v2 uses different models, so its content-gate verdicts will
  differ — v2 may keep a quiet piano that v1 dropped, or vice versa. **Render the union of
  stem names**, with an explicit "not detected in this version" cell rather than an empty
  player or a missing row. A silently absent row reads as a bug.

### Empty-stem messaging (v1 and v2 alike)

Now that stem counts vary per track, the UI must explain it once, near the stem list:
*"We only return stems we detected in your track."* Without that line, a user who
uploaded a piano ballad and got 3 files will file a bug about the missing 6.

---

## Sources

- [MVSEP Multisong Leaderboard (vocals)](https://mvsep.com/quality_checker/multisong_leaderboard?sort=vocals)
- [MVSEP algorithms page](https://mvsep.com/en/algorithms) — ensemble composition, "Ensemble All-In"
- [ZFTurbo/Music-Source-Separation-Training](https://github.com/ZFTurbo/Music-Source-Separation-Training) — inference, model types, MIT license
- [ZFTurbo pretrained_models.md](https://github.com/ZFTurbo/Music-Source-Separation-Training/blob/main/docs/pretrained_models.md) — SDR table, checkpoint links
- [SCNet XL IHF release v1.0.15](https://github.com/ZFTurbo/Music-Source-Separation-Training/releases/tag/v1.0.15) — 10.089 MUSDB / 9.926 Multisong, MUSDB18-only training
- [SCNet XL release v1.0.13](https://github.com/ZFTurbo/Music-Source-Separation-Training/releases/tag/v1.0.13)
- [KimberleyJSN/melbandroformer (HF)](https://huggingface.co/KimberleyJSN/melbandroformer) — MIT weights
- [KimberleyJensen/Mel-Band-Roformer-Vocal-Model](https://github.com/KimberleyJensen/Mel-Band-Roformer-Vocal-Model) — chunk_size, 40 GB figure is *training*
- [MUSDB18 dataset licensing (SigSep)](https://sigsep.github.io/datasets/musdb.html) and [MUSDB18-HQ on Zenodo](https://zenodo.org/records/3338373)
- [Music Source Separation with Band-Split RoPE Transformer (arXiv 2309.02612)](https://arxiv.org/abs/2309.02612)
- [Mel-Band RoFormer (arXiv 2310.01809)](https://arxiv.org/pdf/2310.01809)
- [Benchmarks and leaderboards for sound demixing tasks (arXiv 2305.07489)](https://arxiv.org/html/2305.07489v2)
- [facebookresearch/demucs](https://github.com/facebookresearch/demucs) — MIT, htdemucs_ft trained on "MusDB + 800 songs"
- [nomadkaraoke/python-audio-separator](https://github.com/nomadkaraoke/python-audio-separator) — MIT, pip-installable RoFormer/Demucs runner; a shortcut worth evaluating for 4.2
- [htdemucs vs BS-RoFormer vs Spleeter 2026 benchmark](https://aistemsplitter.org/blog/htdemucs-vs-bs-roformer-vs-spleeter-2026-benchmark) — runtime/cost, **secondary vendor source**
