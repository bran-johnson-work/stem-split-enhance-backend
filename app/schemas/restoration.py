import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.models.restoration_job import RestorationStage, RestorationStatus


class RestorationStartRequest(BaseModel):
    """Start one stage of the restoration pipeline.

    The two stages are started separately and chained by the caller: run ``v2``,
    wait for it, then run ``v3`` with ``after_job_id`` set to the finished V2 job.
    The V3 run then consumes V2's output instead of the original audio.
    """

    stage: RestorationStage = RestorationStage.V2
    # The finished V2 job whose output feeds this run. V3 only; omit it to run V3
    # straight off the original audio (restoration without the repair stage).
    after_job_id: uuid.UUID | None = None
    # Which input file to start from, when a project holds more than one. Ignored
    # when `after_job_id` is set, because the source is then the parent's output.
    file_id: uuid.UUID | None = None

    # V3 step toggles. Ignored for a V2 run, which has no switchable steps.
    tempo: bool = True
    denoise: bool = True
    bandwidth: bool = True
    # A/B measurement against the input. Off by default — it costs ~10s per minute
    # of audio, which is not worth paying on every run.
    quality: bool = False


class RestorationOutput(BaseModel):
    """A finished artifact. `url` is minted per request and expires."""

    key: str
    url: str
    expires_in: int


class RestorationJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    stage: RestorationStage
    status: RestorationStatus
    parent_id: uuid.UUID | None = None

    # The upstream's step log rather than a percentage — the pipeline does not
    # report one, and inventing a number here would be a lie the UI renders as a bar.
    progress: list[str] = Field(default_factory=list)
    # Convenience for the UI: the last progress line, or "" before the first.
    current_step: str = ""
    is_finished: bool = False

    output: RestorationOutput | None = None
    report: dict[str, Any] | None = None
    quality: dict[str, Any] | None = None
    error_message: str | None = None

    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class RestorationServiceStatus(BaseModel):
    """Whether the restoration service is reachable and what it can do.

    The UI needs this before offering the feature: bandwidth extension is a no-op
    when the Apollo checkout is missing, and a CPU-only host takes long enough that
    it is worth saying so up front rather than after a 20-minute wait.
    """

    enabled: bool
    reachable: bool
    cuda: bool = False
    gpu: str | None = None
    apollo: bool = False
    bucket_configured: bool = False
    queue_depth: int = 0
    detail: str | None = None
