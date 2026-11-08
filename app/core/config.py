from functools import lru_cache
from urllib.parse import quote_plus
from uuid import uuid4

from pydantic_settings import BaseSettings, SettingsConfigDict

# Historical insecure placeholder — treated as "unset" by the production guard.
INSECURE_DEFAULT_SECRET = "your-super-secret-key-change-in-production"


class Settings(BaseSettings):
    app_env: str = "local"
    cors_origins: str = "*"
    log_level: str = "INFO"

    # Legacy — nothing reads these; kept so an existing .env still loads.
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""

    # --- AWS S3 object storage ---
    aws_region: str = "us-east-1"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    # Empty => real AWS; set to an S3-compatible endpoint (LocalStack: :4566) for local dev.
    aws_endpoint_url: str = ""
    # S3 bucket names are globally unique across all AWS accounts — a real deploy suffixes these.
    s3_bucket_inputs: str = "audio-inputs"
    s3_bucket_stems: str = "audio-stems"
    # Profile avatars. Kept private like the others; served via short-lived signed
    # GET URLs surfaced on ``UserResponse.avatar_url``. Blank => reuse the inputs
    # bucket (avatars live under an ``avatars/`` key prefix), so no separate bucket
    # has to be created/CORS-configured — most deploys point every S3_BUCKET_* at
    # one real bucket. Set this only to isolate avatars in their own bucket.
    s3_bucket_avatars: str = ""
    s3_upload_url_expire_seconds: int = 3600
    # Avatar download URL lifetime. Longer than an upload URL because it rides on
    # the (cacheable) user object; 7 days is also the SigV4 presigned-URL maximum.
    avatar_url_expire_seconds: int = 7 * 24 * 3600
    # Per-avatar size ceiling, baked into the presign policy (mirrors the frontend
    # AvatarStore's 2 MB limit).
    max_avatar_size_bytes: int = 2 * 1024 * 1024
    allowed_image_content_types: str = "image/png,image/jpeg,image/webp"

    # --- PostgreSQL (AWS RDS, or any standalone Postgres) ---
    # Set postgres_host to make these take precedence over the legacy DATABASE_URL(_POOLED).
    postgres_type: str = "postgres"
    postgres_host: str = ""
    postgres_port: int = 5432
    postgres_username: str = ""
    postgres_password: str = ""
    postgres_database: str = ""
    # Applies to whichever connection is active, not just the RDS one — see asyncpg_connect_args.
    postgres_ssl: bool = False
    postgres_log: bool = False

    # Legacy Supabase strings, used only when postgres_host is unset: direct (Alembic),
    # pooled = PgBouncer transaction pooler (the app).
    database_url: str = ""
    database_url_pooled: str = ""

    # JWT authentication
    secret_key: str = ""
    jwt_algorithm: str = "HS256"
    jwt_issuer: str = "ai-audio-stem-api"
    jwt_audience: str = "ai-audio-stem-api-clients"
    jwt_email_verification_audience: str = "ai-audio-stem-api-email-verification"
    jwt_password_reset_audience: str = "ai-audio-stem-api-password-reset"
    access_token_expire_minutes: int = 30
    # Refresh-token lifetime. The access token stays short (a stolen one expires
    # fast); this is what actually keeps a user signed in, since the client
    # silently exchanges it for a new access token. 30 days of inactivity ends
    # the session — every refresh issues a new token, so an active user's window
    # keeps moving forward.
    refresh_token_expire_days: int = 30

    # PBKDF2-HMAC-SHA256. OWASP-recommended floor is ~600k.
    pbkdf2_iterations: int = 600_000

    # Login brute-force throttle (per client IP + email, sliding window)
    login_rate_limit_max_attempts: int = 5
    login_rate_limit_window_seconds: int = 300


    signup_rate_limit_max_attempts: int = 10
    signup_rate_limit_window_seconds: int = 3600


    # Fallback only, for when a user's plan can't be resolved. NOT a clamp — the presign
    # policy normally carries the plan's own max_file_size_mb, and clamping here would cut
    # every paid tier down to this Supabase-era 50 MB figure.
    max_upload_size_bytes: int = 50 * 1024 * 1024
    allowed_audio_content_types: str = (
        "audio/mpeg,audio/mp3,audio/wav,audio/x-wav,audio/wave,"
        "audio/flac,audio/x-flac,audio/mp4,audio/aac,audio/ogg,audio/webm"
    )

    # Email (Resend). Empty key => sends become logged no-ops, keeping dev/tests network-free.
    resend_api_key: str = ""
    resend_api_base_url: str = "https://api.resend.com"

    email_from: str = "onboarding@resend.dev"
    email_from_name: str = "AI Audio Stem"
    email_timeout_seconds: float = 10.0
    frontend_url: str = "http://localhost:3000"
    email_verification_token_expire_hours: int = 24
    # Password-reset links are shorter-lived than verification links: a reset grants
    # account takeover, so the window for a leaked link to be abused is kept small.
    password_reset_token_expire_hours: int = 1

    require_email_verification: bool = True

    # --- Billing (Stripe) ---
    # Empty secret key => billing disabled: checkout/portal 503, webhook 404.
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_api_base_url: str = "https://api.stripe.com"
    stripe_timeout_seconds: float = 15.0
    # Empty => fall back to frontend_url.
    billing_success_url: str = ""
    billing_cancel_url: str = ""
    billing_portal_return_url: str = ""

    # --- Stem-separation engine + worker ---
    # auto = CUDA > MPS > CPU. An explicit "cuda"/"cuda:N" REQUIRES the GPU and fails fast
    # at startup rather than silently falling back to CPU (10-50x slower) on a rented box.
    worker_device: str = "auto"
    demucs_model: str = "htdemucs_ft"
    instruments_model: str = "htdemucs_6s"
    enable_instruments: bool = True
    enable_enhancement: bool = True
    worker_poll_interval_seconds: float = 5.0

    # --- Separation-quality knobs (the levers against stem bleed / crosstalk) ---
    # shifts = demucs test-time augmentation: run apply_model N times with random phase
    # shifts and average. This is the single biggest lever for reducing bleed between
    # stems (e.g. vocals leaking into "other"), at ~linear CPU/GPU cost. 1 = fastest/POC,
    # 2-5 = noticeably cleaner, 10 = diminishing returns. Set 2 on a GPU box in prod.
    demucs_shifts: int = 1
    # overlap = fraction of overlap between the analysis chunks apply_model splits into.
    # Higher = smoother seams and slightly better separation; 0.25 is fast, 0.5 is the
    # demucs default and the recommended prod value. Range (0, 1).
    demucs_overlap: float = 0.5
    # Post-separation restoration intensity: "full" | "light" | "off".
    #   full  = the Suno-artifact chain (denoise + declick + exciter + multiband saturation);
    #           great for AI-generated audio, but can over-process clean real recordings.
    #   light = gentle denoise + safety-limit only (most faithful for real mixes).
    #   off   = peak-normalize only (raw separated level).
    # Left as "full" to preserve current behavior; flip to "light" if real-music stems
    # come out over-processed / "messy". `enable_enhancement=False` still forces "off".
    enhancement_level: str = "full"

    # --- Empty-stem filtering (only return stems the track actually contains) ---
    # A separation model emits every source it was trained on, so a solo-piano upload
    # still produced a "drums" file full of bleed. With the gate on, a stem is written
    # only when it clears BOTH floors below. Set False to restore the old fixed stem set.
    stem_filter_enabled: bool = True
    # Absolute floor: a stem never peaking above this is inaudible in any context.
    stem_silence_peak_dbfs: float = -50.0
    # Relative floor: the stem's loudest window vs. the mix's overall RMS. Level-independent,
    # which the absolute floor is not. Raise toward -30 to prune more aggressively; lower
    # toward -45 to keep more. Both floors are permissive by design — dropping a stem the
    # user can hear is far worse than shipping one extra quiet file.
    stem_silence_relative_rms_db: float = -38.0
    # Window the relative measurement uses. Measuring the loudest window (not overall RMS)
    # is what keeps an instrument that plays only in the bridge from being pruned.
    stem_silence_window_seconds: float = 1.0
    # Comma-separated stems that are never dropped, whatever the measurement says — an
    # escape hatch for pinning a stem the product contract promises. Empty = gate them all.
    stem_filter_always_keep: str = ""

    # --- Engine versioning / A-B (see docs/stem-quality-v2.md) ---
    # v1 = HT-Demucs only (the fleet deployed today). v2 = Mel-Band RoFormer vocals +
    # Demucs for the remaining stems.
    #
    # WORKER_ENGINE_VERSIONS is the deployment knob: the versions THIS container claims
    # from the shared queue. The v1 service sets "v1", the v2 service sets "v2". A v2 job
    # claimed by a v1 image would silently run the wrong model and mislabel the output,
    # so this is a correctness boundary. Blank claims anything (single-fleet deploys).
    worker_engine_versions: str = "v1"
    # Which version a newly enqueued job is stamped with.
    engine_version_default: str = "v1"
    # Dual-run: enqueue BOTH a v1 and a v2 job per request so the UI can compare them.
    # Costs two full renders — gate it with the percentage/allowlist below.
    engine_ab_enabled: bool = False
    # Percentage of projects that get dual-run, bucketed on a stable hash of the project
    # id (never per-request random, or a user's project would flip version on retry).
    engine_ab_percent: int = 0
    # User ids that always dual-run regardless of the percentage (the internal team).
    engine_ab_user_allowlist: str = ""

    # --- v2 engine: Mel-Band RoFormer ---
    # NOTE: the top-of-leaderboard "BS Roformer 124 bands (2026.07)" has NO public
    # weights — it is MVSep-hosted only and cannot be self-hosted at any price. This is
    # the best *downloadable* vocal model (MIT weights, ~10.98 dB vocals SDR).
    v2_vocals_ckpt_url: str = (
        "https://huggingface.co/KimberleyJSN/melbandroformer/resolve/main/MelBandRoformer.ckpt"
    )
    v2_vocals_config_url: str = (
        "https://raw.githubusercontent.com/ZFTurbo/Music-Source-Separation-Training/main/"
        "configs/KimberleyJensen/config_vocals_mel_band_roformer_kj.yaml"
    )
    # Pin this once you have fetched the checkpoint (`python -m scripts.fetch_v2_weights`
    # prints it). Blank = the download is NOT integrity-checked, which is logged as a
    # warning on every load: a community checkpoint changing under you is a silent
    # quality regression with no diff to review.
    v2_vocals_ckpt_sha256: str = ""
    # Where v2 checkpoints are cached. Defaults under TORCH_HOME so the existing
    # /cache/torch volume persists them alongside the demucs weights.
    v2_weights_dir: str = ""
    # Inference dials. chunk_size comes from the model config (352800 = 8 s @ 44.1 kHz).
    # overlap = how many chunk-strides cover each sample; higher is smoother at the seams
    # and linearly slower. 2 is the config default, 4 is a good prod value.
    v2_overlap: int = 2
    # Chunks per forward pass. The main VRAM dial: raise it to use a bigger card, drop it
    # to 1 if you hit CUDA OOM.
    v2_batch_size: int = 4
    # Rebuild "other" as (mix - roformer_vocals - demucs_drums - demucs_bass) instead of
    # taking Demucs' own "other". The RoFormer vocal is much cleaner than Demucs', so
    # subtracting it removes the vocal bleed that made "other" the worst v1 stem.
    v2_recompute_other: bool = True

    # --- Progress-write debounce (RCA-3) ---
    # Sample every poll_seconds, but only write on a label change, a jump >= min_delta,
    # or once min_interval_seconds has passed (liveness tick, so long stages don't look frozen).
    worker_progress_poll_seconds: float = 2.0
    worker_progress_min_delta: float = 5.0
    worker_progress_min_interval_seconds: float = 10.0

    # --- Worker lease / stuck-job recovery (RCA-1) ---
    # The reaper reclaims any PROCESSING job whose heartbeat is older than the stale timeout.
    # Keep that timeout several x the heartbeat interval so a missed beat isn't a false reclaim.
    worker_heartbeat_interval_seconds: float = 15.0
    job_stale_timeout_seconds: float = 90.0
    reaper_interval_seconds: float = 30.0
    # Safe to leave on everywhere (the sweep locks rows SKIP LOCKED).
    worker_run_reaper: bool = True
    job_scratch_dir: str = ""
    # When set, stems are written here instead of uploaded, mirroring the bucket layout:
    # <dir>/<user_id>/<project_id>/stems/<name>.wav.
    stems_local_dir: str = ""

    # --- Restoration service (the V2/V3 enhancement pipeline, a separate GPU host) ---
    # That API is not browser-callable: it has no CORS and authenticates with a static
    # X-API-Key, which cannot be shipped to a frontend. So this backend is the only
    # caller and proxies for the browser. Blank URL disables the feature outright.
    restoration_api_url: str = ""
    restoration_api_key: str = ""
    # Its queue is one job deep on one GPU, so a call can block behind another run.
    # Generous connect/read budget, but never unbounded.
    restoration_timeout_seconds: float = 30.0
    # TTL for the URLs we re-sign for the browser. The upstream mints 1h URLs at job
    # completion; a user with the page open longer than that needs a fresh one.
    restoration_url_expire_seconds: int = 3600
    # How long a `/health` answer from the GPU host is reused for. Every open
    # restoration page re-checks availability on a timer, and each check was its own
    # round trip to a host that is busy running a job. Ten seconds is far below the
    # frontend's own poll interval and far above the rate at which "is there a GPU"
    # can meaningfully change.
    restoration_status_cache_seconds: float = 10.0

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def is_local(self) -> bool:
        return self.app_env.lower() == "local"

    @property
    def has_secure_secret(self) -> bool:
        return bool(self.secret_key) and self.secret_key != INSECURE_DEFAULT_SECRET

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def allowed_audio_content_type_set(self) -> set[str]:
        return {t.strip().lower() for t in self.allowed_audio_content_types.split(",") if t.strip()}

    @property
    def allowed_image_content_type_set(self) -> set[str]:
        return {t.strip().lower() for t in self.allowed_image_content_types.split(",") if t.strip()}

    @property
    def restoration_enabled(self) -> bool:
        """Both halves are required: a URL with no key gets 401 on every call."""
        return bool(self.restoration_api_url.strip() and self.restoration_api_key.strip())

    @property
    def restoration_base_url(self) -> str:
        return self.restoration_api_url.strip().rstrip("/")

    @property
    def worker_engine_version_list(self) -> list[str]:
        """Engine versions this worker claims. Empty list = claim any version."""
        return [v.strip().lower() for v in self.worker_engine_versions.split(",") if v.strip()]

    @property
    def ab_user_allowlist_set(self) -> set[str]:
        return {u.strip().lower() for u in self.engine_ab_user_allowlist.split(",") if u.strip()}

    @property
    def stem_filter_always_keep_set(self) -> frozenset[str]:
        return frozenset(
            n.strip().lower() for n in self.stem_filter_always_keep.split(",") if n.strip()
        )

    @property
    def has_postgres_components(self) -> bool:
        """True when discrete POSTGRES_* settings drive the connection (AWS RDS)."""
        return bool(self.postgres_host)

    def _build_postgres_url(self) -> str:
        """Assemble a SQLAlchemy async URL from the discrete POSTGRES_* components.

        User and password are percent-encoded so an ``@`` or ``/`` in the password
        can't corrupt the URL.
        """
        user = quote_plus(self.postgres_username)
        password = quote_plus(self.postgres_password)
        auth = f"{user}:{password}@" if user else ""
        return (
            f"postgresql+asyncpg://{auth}"
            f"{self.postgres_host}:{self.postgres_port}/{self.postgres_database}"
        )

    @property
    def active_database_url(self) -> str:
        """URL the app connects with: RDS components, else the legacy pooled/direct URL."""
        if self.has_postgres_components:
            return self._build_postgres_url()
        return self.database_url_pooled or self.database_url

    @property
    def migration_database_url(self) -> str:
        """URL Alembic connects with. Same as the app on RDS; prefers the direct URL for DDL."""
        if self.has_postgres_components:
            return self._build_postgres_url()
        return self.database_url or self.database_url_pooled

    @property
    def db_echo(self) -> bool:
        """Whether SQLAlchemy should echo SQL (explicit POSTGRES_LOG, or local dev)."""
        return self.postgres_log or self.is_local

    def asyncpg_connect_args(self) -> dict:
        """connect_args for asyncpg, shared by the app engine and Alembic.

        The Supabase transaction pooler shares a connection per transaction, so it can't
        support asyncpg's default server-side prepared statements — reused connections
        collide on names like ``__asyncpg_stmt_1__``. Do not remove that branch. A direct
        RDS connection (or RDS Proxy, which pins per session) needs none of it.
        """
        args: dict = {}
        if self.postgres_ssl:
            args["ssl"] = True
        if "pooler.supabase.com" in self.active_database_url:
            args.update(
                statement_cache_size=0,
                prepared_statement_cache_size=0,
                prepared_statement_name_func=lambda: f"__asyncpg_{uuid4()}__",
            )
        return args

    @property
    def email_enabled(self) -> bool:
        """Whether outbound email is configured (a Resend API key is present)."""
        return bool(self.resend_api_key)

    @property
    def billing_enabled(self) -> bool:
        """Whether Stripe billing is configured (a secret key is present)."""
        return bool(self.stripe_secret_key)

    @property
    def checkout_success_url(self) -> str:
        return self.billing_success_url or f"{self.frontend_url}/billing/success"

    @property
    def checkout_cancel_url(self) -> str:
        return self.billing_cancel_url or f"{self.frontend_url}/billing"

    @property
    def portal_return_url(self) -> str:
        return self.billing_portal_return_url or f"{self.frontend_url}/billing"

    @property
    def email_uses_sandbox_sender(self) -> bool:
        """True when EMAIL_FROM is Resend's shared sandbox address.

        With it, Resend only delivers to the account owner (others get HTTP 403), so
        verification emails to real signups silently never arrive.
        """
        return self.email_from.strip().lower() == "onboarding@resend.dev"

    @property
    def email_sender(self) -> str:
        """Sender in the ``Name <address>`` form the Resend API expects."""
        if self.email_from_name:
            return f"{self.email_from_name} <{self.email_from}>"
        return self.email_from

    def assert_production_ready(self) -> None:
        """Fail fast on insecure configuration outside local development.

        Called at startup (``app.main``) rather than import time so tests can still
        construct ``Settings`` for other envs.
        """
        if self.is_local:
            return
        if not self.has_secure_secret:
            raise RuntimeError(
                "SECRET_KEY must be set to a strong, unique value in non-local "
                "environments (it is unset or still the insecure default)."
            )
        if "*" in self.cors_origin_list:
            raise RuntimeError(
                "CORS_ORIGINS must list explicit origins in non-local environments; "
                "the '*' wildcard is not allowed in production."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
