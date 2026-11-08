# Demo agenda — audio restoration (V2 + V3)

Runbook for demoing the restoration pipeline end to end: browser upload → GPU
host → results with audio and measurements.

Everything below is marked **Verified** (observed in a real run), **In progress**
(built, not yet exercised on the deployed stack) or **Needs validation** (open
question). Nothing here is estimated and presented as measured — if a number is
not in this document, we have not measured it, and the honest answer during the
demo is "we haven't measured that yet".

---

## 1. What is being shown

One track through a two-stage GPU pipeline that is **not** stem separation:

| Stage | What it does |
|---|---|
| **V2** | Localized repair — finds one damaged span, re-renders it from donor notes elsewhere in the track, and **rolls back if the repair does not lower measured risk** |
| **V3** | Tempo correction → denoise → bandwidth extension, each individually switchable |

They are chained: V3's input is the file V2 writes, so V3 cannot start until V2
finishes. That ordering is the reason they are two separate jobs.

The audio never passes through our API. The browser uploads straight to S3 with a
presigned POST; the two services exchange S3 keys only.

---

## 2. Pre-flight — do this before anyone is watching

Each of these has already broken a run at least once. Budget 10 minutes.

```bash
# 1. GPU host is up, on CUDA, in fp16, and warm
curl -s http://<gpu-host>:8000/health | python -m json.tool
```

Required: `"device": "cuda"`, `"precision": "fp16"`, `"warm": true`, `"apollo": true`.

- `device` is not the same claim as `cuda` — a host with a GPU pinned to CPU
  reports `cuda: true` and still runs everything on the CPU.
- `warm: false` means checkpoints are still downloading. The first run will take
  minutes longer. **Wait for `true`.**
- `apollo: false` means bandwidth extension will fail — the UI disables the
  toggle, so the demo silently loses its most visible step.

```bash
# 2. HF token present — the MuScriptor weights are gated
grep -q '^HF_TOKEN=..' .env && echo OK || echo "MISSING — Stage A will fail mid-job"
```

```bash
# 3. Nothing else is holding port 8000 (Streamlit on 8000 breaks the API entirely)
ss -lntp | grep ':8000'
```

```bash
# 4. Backend can actually reach it
curl -s https://<api-host>/api/v1/restoration/status -H "Authorization: Bearer <token>"
```

Then **run one full track through before the demo**. Not a dry run of the UI — a
real job. It warms the models, proves the chain, and gives you a known-good result
to fall back on.

---

## 3. Running order

**Total run time is not yet measured end to end.** See §5 — say "a few minutes"
and do not quote a number you have not timed.

### Step 1 — Frame it (30s)
Separation splits a track into stems. This is the other pipeline: it repairs and
enhances a single piece of audio. Different problem, different models.

### Step 2 — Upload (30s)
Point out the upload goes **straight to S3**, not through the API. The progress
bar here is real byte progress.

### Step 3 — The wait (the honest part)
This is GPU work and it takes minutes. What the UI shows while it runs:

- a progress bar driven by the pipeline's own named steps
- the current step label, updating as the pipeline reports it
- an elapsed timer
- **queue position** when another run is ahead — the GPU processes one job at a time

Worth saying out loud: the bar is an estimate derived from real milestones, it
never reaches 100% until the job genuinely finishes, and it never goes backwards.

### Step 4 — The result (the payoff)
Three things land together:

1. **Audio players** — Original / V2 / V3, each pausing the others on play
2. **Waveform comparison** — three lanes on one shared scale, original ghosted
   behind each result, with a measured summary per lane
   (e.g. *"identical to the original"* vs *"47.4% different · +2.6 dB peak"*)
3. **Spectrum plot** — all three on one log-frequency axis; where V3 extends past
   the others is rebuilt high end
4. **Decision cards** — what each stage decided and why

The measurements are computed in the browser from the rendered audio. Nothing is
simulated.

---

## 4. Verified numbers

Observed on the deployed T4 host, single run, ~183s of audio:

| Step | Measured |
|---|---|
| MuScriptor transcription (V2) | **74.5s** for 182.7s of audio |
| Denoise separation (V3) | **29s** for 184.8s of audio |
| Denoise model load | 3s |
| Sample rate | 48000 Hz, 1 → 1 channel |
| Cutoff before → after | 21598 Hz → 21703 Hz |
| V2 outcome on that track | **Unchanged** — repair rolled back |

**Do not extrapolate these into a total.** Separation, analysis, tempo and
bandwidth were not individually timed.

---

## 5. Known weak spots — decide your handling before the demo

**V2 will probably show "Unchanged".** On the test track it flagged 0.0–0.8s,
attempted a repair, measured risk going **up** (0.045 → 0.855) and correctly rolled
back. That is the safety mechanism working — but as a demo moment it is a stage
that visibly did nothing after minutes of GPU time.

Two options, pick one deliberately:
- **Lead with the rollback as a feature**: "it refuses to ship a repair it cannot
  prove is better." Strong story, requires you to say it confidently.
- **Turn V2 off** and demo V3 alone. Halves the wait and every step visibly does
  something. Recommended if the audience is watching a clock.

If you want V2 to actually fire, you need audio with a genuinely damaged span.
A clean track will keep rolling back — by design.

**Run length is linear in audio duration.** A 30–45s clip demos the same pipeline
in a fraction of the time. Use a short clip unless full-length is the point.

**One job at a time.** Do not let two people upload simultaneously; the second
queues behind the first. The UI shows the queue position, which is honest but slow.

---

## 6. If it breaks

| Symptom | Cause | Action |
|---|---|---|
| "Restoration service unavailable" | API not running, or something else on port 8000 | `ps -ef \| grep uvicorn`; check nothing else holds the port |
| Job fails early in Stage A | `HF_TOKEN` missing — gated weights | Cannot fix live. Fall back to the pre-run result |
| Job fails partway through V3 | Check the step log for the failing step | Fall back |
| Bandwidth toggle disabled | Apollo checkout missing on the host | Demo tempo + denoise, say bandwidth needs a model checkout |
| Comparison plots don't draw | S3 CORS on the stems bucket | Players and metrics still work — carry on with those |

**Always have the pre-run result open in a second tab.** It is the difference
between a recoverable hiccup and a dead demo.

---

## 7. Likely questions

**"How long does it take?"** — Honest answer: it depends on track length, and
we've measured the individual heavy steps (§4) but not a full end-to-end run yet.
Do not invent a total.

**"Is it using the GPU?"** — Yes, and `/health` reports the device and precision
so it is verifiable rather than asserted. Separation runs in fp16 on the T4's
tensor cores; bandwidth extension stays fp32 because cuFFT does not support
Apollo's 882-sample transform in half precision.

**"Is the result better?"** — The plots show what changed. Whether it is *better*
is a separate measurement (`scripts/ab_quality.py`, and the optional in-run quality
check). Do not claim improvement the run did not measure.

**"Can it scale?"** — One GPU, one job at a time, today. Concurrency means more
hosts. That is a known constraint, not an oversight — two runs do not fit in the
card's memory.

**"What's next?"** — Batching Apollo's chunks (currently one forward pass at a
time, which leaves the T4 idle between kernels), and replacing polling with pushed
progress.

---

## 8. Status summary

**Verified**
- End-to-end chain runs on the deployed T4: upload → V2 → V3 → signed result URLs
- Transcription and denoise timings (§4)
- V2's rollback safety mechanism fires correctly on clean audio
- GPU in use, fp16 on separation, reported by `/health`

**In progress** — built, not yet exercised on the deployed stack
- Progress bar, elapsed timer, queue position in the UI
- Waveform and spectrum comparison (verified against synthetic data and in a real
  browser; **not** yet against real audio fetched from a signed S3 URL)
- Startup HuggingFace token check

**Needs validation**
- Full end-to-end run time, per step
- Whether the restoration measurably improves the audio, on a corpus rather than
  one track
- Behaviour with a genuinely damaged input, where V2 should engage rather than roll back

**Open risks**
- GPU host port 8000 is reachable from the public internet; `/health` is
  unauthenticated
- Upstream keeps jobs in memory; a restart mid-run loses them (our DB records the
  row and marks it failed rather than spinning)
