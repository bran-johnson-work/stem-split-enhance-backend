# Stem separation engine + worker

How the AI pipeline (migrated from the POC) is wired into this backend, and how it
runs against Supabase.

## Architecture

Supabase is the **coordination + storage backbone**, not the compute host. Demucs/torch
cannot run on Supabase (Edge Functions are Deno/CPU-only with short timeouts and no GPU),
so the AI runs in a separate **worker** process that talks to Supabase.

```
        Supabase (managed)
  ┌────────────────────────────────────────────┐
  │ Postgres: projects / processing_jobs /      │  ← processing_jobs is ALSO the queue
  │           output_files                       │
  │ Storage:  audio-inputs / audio-stems         │
  │ Auth                                         │
  └───────▲───────────────────────────▲─────────┘
          │ enqueue + read state       │ claim + read/write files
   ┌──────┴────────┐            ┌───────┴───────────────┐
   │  FastAPI API   │  PENDING  │  Worker (CPU/GPU host) │
   │  app.main      │  ───────► │  python -m app.worker  │
   │  (no torch)    │           │  app.engine (torch)    │
   └───────────────┘            └───────────────────────┘
```

- **API** (`app.main`) creates a `processing_jobs` row (`PENDING`) and serves job status +
  signed stem URLs. It never imports `app.engine`, so the API image stays torch-free.
- **Worker** (`app.worker`) claims the oldest `PENDING` job with `SELECT … FOR UPDATE SKIP
  LOCKED` (Postgres *is* the queue — no Redis), downloads the input from `audio-inputs`,
  runs `app.engine`, uploads stems to `audio-stems`, and records `output_files`.

## Request flow

1. `POST /api/v1/projects` → create project.
2. `POST /api/v1/projects/{id}/upload-url` (or `/files/upload-url`) → presigned URL; client
   PUTs the file straight to Supabase.
3. `POST /api/v1/projects/{id}/upload-complete` → records the file, project → `QUEUED`.
4. **`POST /api/v1/projects/{id}/process`** → inserts a `PENDING` job (idempotent while one
   is in flight). Returns the job.
5. Worker claims it → project `PROCESSING`, job progresses 0→100.
6. `GET /api/v1/projects/{id}/jobs/{job_id}` → poll status/progress.
7. `GET /api/v1/projects/{id}/jobs/{job_id}/stems` → signed download URLs for each stem.

Stems land at `audio-stems/{user_id}/{project_id}/stems/{stem}.wav`, so the existing
`delete_project` prefix purge already cleans them up.

## Stems produced

`vocals, drums, kick, snare, hihats, bass, guitar, piano, other` (9). `htdemucs_ft` gives
vocals/drums/bass/other; `htdemucs_6s` adds guitar/piano; the drums stem is DSP band-split
into kick/snare/hihats. Enhanced tonal stems have `is_enhanced=true`; the drum sub-stems are
peak-normalized only (`is_enhanced=false`).

## Running the worker

No new migrations are needed — `processing_jobs` and `output_files` already exist.

**Local (native):**
```bash
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements-worker.txt
python -m app.worker            # uses the same .env as the API
```

**Docker (CPU):**
```bash
docker build -f Dockerfile.worker -t stem-worker .
docker run --rm --env-file .env -v stem_torch:/cache/torch stem-worker
```

The worker needs the same env as the API: `DATABASE_URL_POOLED`, `SUPABASE_URL`,
`SUPABASE_SERVICE_ROLE_KEY` (plus the engine vars in `.env.example`). Scale out by running
more worker instances — `FOR UPDATE SKIP LOCKED` guarantees each job goes to exactly one.

## Deployment targets

- **Supabase**: Postgres + Storage + Auth (already your setup). Ensure the `audio-inputs`
  and `audio-stems` buckets exist (see `app/core/storage.ensure_bucket`).
- **API**: any PaaS (Render / Railway / Fly). Connects over the pooled URL.
- **Worker**: a CPU host for the MVP (a 3–4 min track takes a few minutes). Move to a GPU
  host later with **no code change** — the device auto-selects CUDA. Persist `TORCH_HOME`
  so the ~400 MB weights download only once.

## Notes / future work

- Progress is relayed to the DB ~1×/s while the engine runs in a worker thread; the client
  polls. Swap polling for SSE/WebSockets later if desired.
- Reference-guided enhancement (`ref_window`) is supported by the engine but not yet exposed
  on `/process`; add it as an optional request field when needed.
- Raw (pre-enhancement) A/B stems from the POC are not stored (one deliverable per stem) —
  the `is_enhanced` flag and the pipeline already support adding them if the UI wants A/B.
- The queue is polling-based (`WORKER_POLL_INTERVAL_SECONDS`). For lower latency, switch to
  `LISTEN/NOTIFY` or Supabase Queues (`pgmq`) without changing the claim semantics.
```
