# Deploying two worker fleets: v1 (HT-Demucs) and v2 (Mel-Band RoFormer)

You were running **4 workers of one image**. This splits that into **two services from two
images**, sharing one Postgres queue:

| Service | Image | Claims | Models |
|---|---|---|---|
| `worker-v1` | `Dockerfile.worker` | `engine_version = v1` | htdemucs_ft + htdemucs_6s (unchanged) |
| `worker-v2` | `Dockerfile.worker.v2` | `engine_version = v2` | Mel-Band RoFormer vocals + htdemucs_ft rhythm + htdemucs_6s |

## About the model you asked for

**"BS Roformer 124 bands (2026.07.10, MVSep)" cannot be deployed in a container.** It is
the leaderboard leader (12.33 dB vocals) but it is an **MVSep-hosted service
configuration** — the weights have never been published. No self-hosted deployment can
reach it; the only access is MVSep's API.

v2 therefore runs **Mel-Band RoFormer** (KimberleyJensen, MIT weights, ~10.98 dB vocals
SDR) — the best *downloadable* vocal model. Against the v1 baseline of ~8.2–8.9 dB that is
still the large majority of the available gain.

The model is config-driven (`V2_VOCALS_CKPT_URL` + `V2_VOCALS_CONFIG_URL`), and the loader
reads hyperparameters from the checkpoint's own config YAML rather than hardcoding them —
so swapping in a different RoFormer checkpoint later is an env change, not a code change.

## How the two fleets stay separated

One mechanism, and only one: **`WORKER_ENGINE_VERSIONS`**.

```
processing_jobs                    worker-v1  (WORKER_ENGINE_VERSIONS=v1)
┌──────────────┬────────────────┐    claims WHERE engine_version IN ('v1')
│ id           │ engine_version │  ◄─┘
├──────────────┼────────────────┤  worker-v2  (WORKER_ENGINE_VERSIONS=v2)
│ …            │ v1             │    claims WHERE engine_version IN ('v2')
│ …            │ v2             │  ◄─┘
└──────────────┴────────────────┘
```

Both fleets poll the same table with `FOR UPDATE SKIP LOCKED`, filtered on that list
(`ProcessingJobRepository.claim_next_pending`). This is a **correctness boundary, not an
optimization**: a v1 image has no RoFormer weights, so a v2 job claimed by a v1 worker
would run HT-Demucs and record the output as v2 — wrong audio under a label the user
trusts. **Never widen the v1 service to `v1,v2`.**

Scaling the fleets is independent. Nothing else routes work.

## Rollout order

Deploy in this order; each step is safe on its own.

### 1. Migrate the database (before either image ships)

```bash
alembic upgrade head    # b8c2e4f6a1d3 — adds engine_version + engine_config
```

`engine_version` lands with `server_default 'v1'`, so every existing row backfills to `v1`
and stays claimable by the fleet you already have. **Backward compatible in both
directions**: the currently-deployed worker code ignores the new columns, so you can run
the migration before deploying any new image.

### 2. Redeploy the v1 fleet (no behaviour change)

Rebuild `Dockerfile.worker` from this commit with `WORKER_ENGINE_VERSIONS=v1`. Same models,
same output — this step only teaches the existing fleet to filter its claims. Confirm:

```
worker.started poll=5.0s engine_versions=['v1']
```

Verify normal jobs still complete before continuing. At this point nothing enqueues `v2`,
so the v2 fleet has nothing to do yet — deploy it in either order.

### 3. Fetch and pin the v2 weights

```bash
python -m scripts.fetch_v2_weights
```

Prints the checkpoint's SHA-256. **Put it in `V2_VOCALS_CKPT_SHA256`.** Unpinned, the
loader warns on every load and a silent upstream re-publish becomes an unexplained quality
change weeks later instead of a hard failure at load time.

For a fleet that scales, bake the weights into the image instead of downloading per cold
start — add to `Dockerfile.worker.v2`:

```dockerfile
RUN python -m scripts.fetch_v2_weights
```

### 4. Deploy the v2 fleet

```bash
docker compose -f docker-compose.workers.yml build
docker compose -f docker-compose.workers.yml up -d
```

Defaults to `2 × v1 + 2 × v2`, keeping your total at 4. Tune with `WORKER_V1_REPLICAS` /
`WORKER_V2_REPLICAS`.

The v2 container sets `WORKER_ENGINE_VERSIONS=v2` in the image itself, so it cannot claim
v1 work even if the env is misconfigured. On boot it prewarms both stacks and surfaces a
bad checkpoint immediately rather than on a customer's job:

```
worker.prewarm.start versions=['v2']
roformer.ready device=cuda
worker.started poll=5.0s engine_versions=['v2']
```

### 5. Turn on A/B for yourselves only

```bash
ENGINE_AB_ENABLED=true
ENGINE_AB_PERCENT=0
ENGINE_AB_USER_ALLOWLIST=<your-user-uuid>
```

Every `POST /process` from an allowlisted user now enqueues **two** jobs. Compare them:

```
GET /api/v1/projects/{id}/stems/compare
```

Then widen: `ENGINE_AB_PERCENT=5` → `25`. Bucketing is a stable SHA-256 of the project id,
so a project never flips sides between retries.

### 6. Promote (only on listening data)

```bash
ENGINE_VERSION_DEFAULT=v2
ENGINE_AB_ENABLED=false
```

Then rebalance replicas toward v2. Keep the v1 fleet running for a while — old jobs
requeued by the reaper still carry `engine_version = v1` and need somewhere to run.

**Do not promote on SDR alone.** SDR is a proxy that disagrees with listeners often enough
to matter, and the enhancement chain sits downstream of it.

## Rollback

| Situation | Action | Effect |
|---|---|---|
| v2 output is bad | `ENGINE_AB_ENABLED=false`, `ENGINE_VERSION_DEFAULT=v1` | new jobs are v1 only; no redeploy |
| v2 fleet is crashing | scale `worker-v2` to 0 | v2 jobs sit `PENDING`; v1 keeps delivering |
| after promotion | `ENGINE_VERSION_DEFAULT=v1` | immediate, config-only |

**Fallback is structural, not retry logic.** Dual-run creates two independent job rows, so
a v2 failure leaves the v1 job untouched — the user still gets stems, and there is no
half-written output set to clean up. Nothing needs to catch an exception for this to work.

## Capacity and sizing

v2 costs roughly **2× v1 wall-clock** (a RoFormer vocal pass plus the Demucs rhythm pass).
Under A/B every project is rendered twice, so total compute is ~3× a v1-only baseline.
Budget for that before raising `ENGINE_AB_PERCENT`.

**GPU.** Build with the torch index matching the host driver (torch 2.11 ships cu126 /
cu128):

```bash
docker build -f Dockerfile.worker.v2 \
  --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu126 \
  -t stem-worker:v2-gpu .
```

Run with `--gpus all` and `WORKER_DEVICE=cuda` — set it explicitly, not `auto`, so
`_pick_device` fails fast instead of silently running 10–50× slower on CPU.

**VRAM is unmeasured.** The figures circulating for these models (40 GB for Mel-Band, A6000
48 GB in the upstream docs) are **training** numbers. Inference at `chunk_size=352800`
should fit well inside 24 GB and probably 12 GB, but that is an estimate — **measure it on
one instance before sizing the fleet.** Starting target: 24 GB (`g5.xlarge` A10G or
`g6.xlarge` L4). The dials if you hit OOM, in order: `V2_BATCH_SIZE` (4 → 1), then
`V2_OVERLAP` (costs seam quality).

**Reaper timeout.** The compose file gives v2 `JOB_STALE_TIMEOUT_SECONDS=300` against the
90 s default. Too tight and the reaper reclaims jobs that are merely slow, which loops
forever and burns GPU. The reaper itself is version-agnostic and safe to leave on
everywhere — it only requeues, and a requeued job keeps its `engine_version`, so it returns
to the correct fleet.

## Storage layout

```
audio-stems/{user_id}/{project_id}/stems/{stem}.wav        ← v1 (unchanged)
audio-stems/{user_id}/{project_id}/stems/v2/{stem}.wav     ← v2
```

v1 deliberately keeps the historical un-versioned prefix so already-delivered stems and
their signed URLs stay valid. Both live under `{user_id}/{project_id}/`, so the existing
`delete_project` prefix purge still reaches everything.

## What v2 actually changes in the output

- **vocals** — Mel-Band RoFormer instead of htdemucs_ft. The main quality gain.
- **other** — with `V2_RECOMPUTE_OTHER=true` (default), rebuilt as
  `mix − vocals − drums − bass` rather than taken from Demucs. Demucs' own `other` carries
  its vocal-separation error as bleed, which is what made it the worst v1 stem;
  subtracting the much cleaner RoFormer vocal removes it.
- **drums / bass / guitar / piano / kick / snare / hihats** — unchanged (Demucs).
- **Stem count still varies per track.** The content gate runs for both versions, and the
  two can legitimately disagree about which stems exist — different models reach different
  verdicts. `stem_names` on the compare endpoint is the **union**, so the UI can render an
  explicit "not detected in this version" cell instead of a missing row. See
  [empty-stem-filter.md](empty-stem-filter.md).

`processing_jobs.engine_config` records what each run actually used (models, overlap, batch
size, silence thresholds), so a comparison stays interpretable after the settings move on.

## Verification status

Verified here: version-filtered claiming, A/B bucketing and dual-enqueue, the overlap-add
reconstruction math (round-trips to within 1e-4 for overlap 1/2/4, edges un-faded, no
NaN), checkpoint-wrapper normalization, config validation, version dispatch, the migration
chain, and the compare endpoint's OpenAPI contract — 40 tests, plus `ruff` clean.

**Not verified here: real inference against the real checkpoint.** This machine has no GPU
and the tests use a stub model, so the first genuine check is step 3 followed by one real
job. Watch for `roformer.state_dict_partial` in the logs — a large missing-key count means
the config and checkpoint are not a matching pair, and the loader raises past 10%.
