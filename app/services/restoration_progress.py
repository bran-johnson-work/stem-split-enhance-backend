"""A completion fraction derived from the restoration pipeline's step log.

The upstream emits labelled steps, not a percentage, and the honest reading of
that used to be "show the label, not a bar". That is right about the data and
wrong about the user: a V2+V3 run is two GPU stages that together take minutes,
and a line of text that changes every ninety seconds is indistinguishable from a
hung job. People reload the page, start the run again, and queue a second job
behind the first one on a GPU that runs one at a time.

So this maps the steps onto milestones. It is an *estimate* and it is built to
fail in the safe direction:

  * **Monotonic.** The fraction never goes backwards, whatever order steps arrive
    in — a bar that retreats reads as a fault.
  * **Never 100 until finished.** It caps below the last milestone while the job
    is running, so "done" is reported by the job's status and nothing else.
  * **Milestone-based, not time-based.** No countdown is invented. A step that
    takes four times as long as usual stalls the bar at a real, named milestone
    rather than running a clock to zero and sitting there.

The weights are the observed share of wall clock each step takes on a GPU host,
not equal slices: separation and bandwidth extension dominate, and slicing evenly
would park the bar at 40% for most of the run.
"""

from __future__ import annotations

from app.models.restoration_job import RestorationStage, RestorationStatus

# (fragment to look for in a step label, fraction complete once it is reached).
# Matched case-insensitively as a substring, because the upstream interpolates
# filenames and timings into most of its labels.
_V2_MILESTONES: list[tuple[str, float]] = [
    ("fetching source", 0.03),
    ("stem separation", 0.10),
    ("midi transcription", 0.40),
    ("analysis", 0.62),
    ("degradation detection", 0.70),
    ("midi repair", 0.78),
    ("re-render", 0.86),
    ("crossfade splice", 0.93),
    ("measuring quality", 0.97),
]

_V3_MILESTONES: list[tuple[str, float]] = [
    ("fetching source", 0.03),
    ("version 3 input", 0.06),
    ("loading", 0.08),
    ("step 1 - tempo", 0.15),
    ("step 2 - loading denoise", 0.30),
    ("step 2 - denoising", 0.35),
    ("step 3 - extending bandwidth", 0.55),
    ("wrote", 0.92),
    ("measuring quality", 0.97),
]

# The ceiling while a job is still running. Reaching 100 and then continuing to
# work is the one thing a progress bar must never do.
_RUNNING_CAP = 0.97

# What a queued job shows. Not zero: the run has been accepted and the upload is
# already done, and a bar pinned at 0 looks like nothing happened.
_QUEUED = 0.02


def _milestones(stage: RestorationStage) -> list[tuple[str, float]]:
    return _V2_MILESTONES if stage is RestorationStage.V2 else _V3_MILESTONES


def estimate(
    stage: RestorationStage, status: RestorationStatus, progress: list[str] | None
) -> int:
    """Percent complete for one stage, 0-100.

    Takes the *furthest* milestone any reported step matches rather than the last
    one, so a trailing "Wrote foo.wav" after a bandwidth pass cannot pull the bar
    back to an earlier weight.
    """
    if status is RestorationStatus.DONE:
        return 100
    if status is RestorationStatus.FAILED:
        # Whatever it reached is where it stopped. Snapping to 0 would erase the
        # fact that it got most of the way, which is the useful part of a failure.
        pass

    table = _milestones(stage)
    reached = _QUEUED
    for line in progress or []:
        lowered = line.lower()
        for fragment, fraction in table:
            if fragment in lowered:
                reached = max(reached, fraction)

    if status is RestorationStatus.FAILED:
        return int(round(reached * 100))
    return int(round(min(reached, _RUNNING_CAP) * 100))


def overall(
    v2_percent: int | None, v3_percent: int, ran_repair: bool
) -> int:
    """The two chained stages as one bar.

    V2 is the longer stage on typical material, so the split is 55/45 rather than
    half each — an even split makes the second half of the run appear to crawl.
    A V3-only run (no repair stage) is simply V3's own number.
    """
    if not ran_repair or v2_percent is None:
        return v3_percent
    return int(round(v2_percent * 0.55 + v3_percent * 0.45))


__all__ = ["estimate", "overall"]
