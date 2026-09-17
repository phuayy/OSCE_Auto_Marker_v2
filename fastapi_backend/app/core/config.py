from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from app.core.env import read_bool_env, read_csv_env, read_float_env, read_int_env


def load_env_file(root_dir: Path, file_name: str = ".env") -> bool:
    env_path = root_dir / file_name
    if not env_path.exists():
        return False

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key.replace("_", "").isalnum() or key[0].isdigit():
            continue
        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1]
        if os.getenv(key) in {None, ""}:
            os.environ[key] = value
    return True


PROJECT_ROOT = Path(__file__).resolve().parents[3]
load_env_file(PROJECT_ROOT)

# Where uvicorn listens. Loopback is the development default: the Vite dev
# server proxies to it and nothing else on the network can reach a half-set-up
# instance. A deployment that other devices upload to binds 0.0.0.0 (or one
# interface) through API_HOST — see docs/deployment-vm.md. Both the launcher
# (scripts/run_api.py) and Settings read these, so the two cannot disagree.
DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8787
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


def server_bind_from_env() -> tuple[str, int]:
    """``(host, port)`` the API should listen on, from API_HOST / API_PORT.

    Read after the .env file above has been applied, so a launcher that imports
    this module gets the same answer whether the values came from the shell or
    from the file.
    """
    host = os.getenv("API_HOST", "").strip() or DEFAULT_API_HOST
    return host, read_int_env("API_PORT", DEFAULT_API_PORT)


def winget_packages_dir() -> Path:
    """Root of winget's per-user package installs; a non-existent path when unset."""
    local_appdata = os.getenv("LOCALAPPDATA", "").strip()
    if os.name != "nt" or not local_appdata:
        return Path("winget-packages-root-not-available")
    return Path(local_appdata) / "Microsoft" / "WinGet" / "Packages"


def resolve_binary_from_candidates(
    env_value: str | None,
    fallback_command: str,
    candidates: list[Path] | None = None,
) -> str:
    explicit_value = str(env_value or "").strip()
    if explicit_value:
        explicit_path = Path(explicit_value).expanduser()
        has_path_marker = explicit_path.is_absolute() or any(separator in explicit_value for separator in ("/", "\\"))
        if has_path_marker:
            return str(explicit_path)
        if shutil.which(explicit_value):
            return explicit_value

    for candidate in candidates or []:
        if candidate.exists():
            return str(candidate)

    return explicit_value or fallback_command


# Name of the panel sub-directory under ``scores/``. One string in one place:
# it is both the filesystem segment and the URL segment the /media/scores mount
# serves a panel's sheets at.
SCORES_PANEL_SUBDIRECTORY = "panel"


@dataclass(frozen=True)
class StoragePaths:
    root_dir: Path
    # Provide storage_root_override to redirect ALL data storage outside the project tree.
    # Set via the STORAGE_ROOT env var; defaults to {root_dir}/storage.
    storage_root_override: Path | None = None

    @property
    def storage_root(self) -> Path:
        return self.storage_root_override if self.storage_root_override is not None else self.root_dir / "storage"

    @property
    def sessions_dir(self) -> Path:
        return self.storage_root / "sessions"

    @property
    def input_videos_dir(self) -> Path:
        return self.storage_root / "input" / "videos"

    @property
    def input_case_studies_dir(self) -> Path:
        return self.storage_root / "input" / "case_studies"

    @property
    def input_rubrics_dir(self) -> Path:
        return self.storage_root / "input" / "rubrics"

    @property
    def output_audio_dir(self) -> Path:
        return self.storage_root / "output" / "audio"

    @property
    def output_audio_professionalism_dir(self) -> Path:
        return self.storage_root / "output" / "audio_professionalism"

    @property
    def output_whisperx_dir(self) -> Path:
        return self.storage_root / "output" / "whisperx"

    @property
    def output_transcripts_dir(self) -> Path:
        return self.storage_root / "output" / "transcripts"

    @property
    def output_scores_dir(self) -> Path:
        return self.storage_root / "output" / "scores"

    @property
    def output_scores_panel_dir(self) -> Path:
        """Parent of each panel session's own directory (``<here>/<session id>``):
        one file per marker, ``adjudication.json``, and the markers' checkpoint
        sidecars. Spelled in the storage layout rather than only in the marking
        package because two layers own it — the strategy writes it, session
        teardown removes it — and a service must not import a pipeline strategy
        to learn where a session's own files are."""
        return self.output_scores_dir / SCORES_PANEL_SUBDIRECTORY

    @property
    def output_case_study_rubrics_dir(self) -> Path:
        """Rubrics extracted from case-study PDFs, keyed by a digest of the PDF's
        bytes (see ``scripts/case_study_rubric.py``).

        Not per session: the point is that the markers of one panel, and every
        clip child of one long recording, share a single extraction. Entries are
        content-addressed and small, so nothing here goes stale and nothing has
        to be swept — deleting the directory only costs the next run one parse."""
        return self.storage_root / "output" / "case_study_rubrics"

    @property
    def output_llm_preprocess_dir(self) -> Path:
        return self.storage_root / "output" / "llm_preprocess"

    @property
    def output_communication_scores_dir(self) -> Path:
        return self.storage_root / "output" / "communication_scores"

    @property
    def output_clips_dir(self) -> Path:
        return self.storage_root / "output" / "clips"

    @property
    def auth_dir(self) -> Path:
        return self.storage_root / "auth"

    @property
    def uploads_dir(self) -> Path:
        return self.storage_root / "uploads"

    @property
    def jobs_dir(self) -> Path:
        return self.storage_root / "jobs"

    @property
    def database_dir(self) -> Path:
        return self.storage_root / "database"

    @property
    def database_path(self) -> Path:
        return self.database_dir / "osce_marker.sqlite3"

    @property
    def credentials_path(self) -> Path:
        return self.auth_dir / "credentials.json"

    @property
    def auth_secret_path(self) -> Path:
        return self.auth_dir / "secret.key"

    @property
    def secrets_path(self) -> Path:
        return self.auth_dir / "secrets.json"

    @property
    def communication_rubric_pdf_path(self) -> Path:
        return self.auth_dir / "communication_rubric.pdf"

    @property
    def communication_rubric_json_path(self) -> Path:
        return self.auth_dir / "communication_rubric.json"

    @property
    def default_rubric_source_pdf(self) -> Path:
        return self.root_dir / "rubrics" / "PHR1012 OSCE Rubric.pdf"

    @property
    def bell_sample_path(self) -> Path:
        return self.storage_root / "input" / "bell_sample.wav"

    def ensure_layout(self) -> None:
        for directory in [
            self.storage_root,
            self.sessions_dir,
            self.input_videos_dir,
            self.input_case_studies_dir,
            self.input_rubrics_dir,
            self.output_audio_dir,
            self.output_audio_professionalism_dir,
            self.output_whisperx_dir,
            self.output_transcripts_dir,
            self.output_scores_dir,
            self.output_case_study_rubrics_dir,
            self.output_llm_preprocess_dir,
            self.output_communication_scores_dir,
            self.output_clips_dir,
            self.auth_dir,
            self.uploads_dir,
            self.jobs_dir,
            self.database_dir,
        ]:
            directory.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Settings:
    backend_root: Path = Path(__file__).resolve().parents[2]
    root_dir: Path = Path(__file__).resolve().parents[3]
    api_host: str = os.getenv("API_HOST", "").strip() or DEFAULT_API_HOST
    api_port: int = read_int_env("API_PORT", DEFAULT_API_PORT)
    # Serve the built frontend (``npm run build`` -> dist/) from this process,
    # so a single-box deployment needs no second web server: the browser loads
    # the app and calls /api and /media on the same origin, which is also what
    # keeps CORS out of the picture. Off in development, where Vite serves the
    # sources and proxies to this API instead. FRONTEND_DIST_DIR points at a
    # build kept outside the checkout.
    serve_frontend: bool = read_bool_env("SERVE_FRONTEND", False)
    frontend_dist_dir_override: str = os.getenv("FRONTEND_DIST_DIR", "").strip()
    max_video_upload_mb: int = read_int_env("MAX_VIDEO_UPLOAD_MB", 2048)
    max_case_study_upload_mb: int = read_int_env("MAX_CASE_STUDY_UPLOAD_MB", 50)
    auth_token_ttl_seconds: int = read_int_env("AUTH_TOKEN_TTL_SECONDS", 60 * 60 * 8)
    default_admin_username: str = os.getenv("DEFAULT_ADMIN_USERNAME", "admin")
    default_admin_password: str = os.getenv("DEFAULT_ADMIN_PASSWORD", "")
    # Optional: gives the bootstrap admin an address so a password reset can
    # reach it. Without one the admin can still sign in and set an address later.
    default_admin_email: str = os.getenv("DEFAULT_ADMIN_EMAIL", "").strip().lower()
    auth_bcrypt_rounds: int = read_int_env("AUTH_BCRYPT_ROUNDS", 12)

    # --- accounts and the emailed links that activate them -----------------
    # Where the links in an invitation or a password-reset email point. Must
    # be the origin the browser loads the app from (https://osce.example.edu);
    # the development default is the Vite dev server.
    app_public_url: str = os.getenv("APP_PUBLIC_URL", "").strip() or "http://localhost:5173"
    # How long an invitation link may sit unread in an inbox, and how long a
    # password-reset link may. Days for one, minutes for the other: an
    # invitation is expected to wait for someone's next working day; a reset
    # is asked for and used in the same sitting.
    invite_token_ttl_hours: int = read_int_env("INVITE_TOKEN_TTL_HOURS", 72)
    password_reset_token_ttl_minutes: int = read_int_env("PASSWORD_RESET_TOKEN_TTL_MINUTES", 30)
    # Whether the admin screen may show an invitation link to copy. Unset, it
    # follows the mail backend: shown when nothing can deliver the email
    # (console), hidden once a relay is configured. "true"/"false" overrides.
    invite_link_visible_to_admin: str = os.getenv("INVITE_LINK_VISIBLE_TO_ADMIN", "").strip().lower()
    # Per-IP throttle on the public token endpoints (accept an invitation,
    # request/confirm a password reset). The tokens are unguessable; this is
    # for the mailbox-flooding and hammering cases.
    token_rate_limit_max_attempts: int = read_int_env("TOKEN_RATE_LIMIT_MAX_ATTEMPTS", 20)
    token_rate_limit_window_seconds: int = read_int_env("TOKEN_RATE_LIMIT_WINDOW_SECONDS", 600)

    # --- outbound email ---------------------------------------------------
    # console (the default) writes each message to the server log instead of
    # sending it; smtp delivers through a relay with aiosmtplib.
    email_backend: str = os.getenv("EMAIL_BACKEND", "console").strip().lower() or "console"
    email_from: str = os.getenv("EMAIL_FROM", "").strip()
    smtp_host: str = os.getenv("SMTP_HOST", "").strip()
    smtp_port: int = read_int_env("SMTP_PORT", 587)
    smtp_username: str = os.getenv("SMTP_USERNAME", "").strip()
    smtp_password: str = os.getenv("SMTP_PASSWORD", "")
    smtp_starttls: bool = read_bool_env("SMTP_STARTTLS", True)
    smtp_use_tls: bool = read_bool_env("SMTP_USE_TLS", False)
    smtp_timeout_seconds: float = read_float_env("SMTP_TIMEOUT_SECONDS", 15.0)
    # Master key for the provider API keys the settings screen stores, as 32
    # bytes in base64 or 64 hex characters. Left empty, the key is derived from
    # this deployment's auth secret (HKDF, separate info label), which keeps a
    # fresh install encrypted at rest with no extra setup. Set it explicitly in
    # production: a key held by the platform's secret store rather than derived
    # from a file next to the database is what makes a stolen database dump
    # useless on its own. Changing it makes existing stored keys unreadable, and
    # the settings screen asks for them to be re-entered.
    credential_encryption_key: str = os.getenv("CREDENTIAL_ENCRYPTION_KEY", "").strip()
    # When True, /media/* static artifacts require a valid bearer token or a
    # short-lived stream ticket. Disable only for fully trusted local setups.
    protect_media_endpoints: bool = read_bool_env("PROTECT_MEDIA_ENDPOINTS", True)
    # Short-lived ticket used by EventSource (SSE) and <video>/<img> media tags,
    # which cannot send an Authorization header. Keeps the long-lived bearer
    # token out of URLs/access logs.
    stream_ticket_ttl_seconds: int = read_int_env("STREAM_TICKET_TTL_SECONDS", 600)
    # Per-IP fixed-window throttle for the login endpoint (brute-force defence).
    login_rate_limit_max_attempts: int = read_int_env("LOGIN_RATE_LIMIT_MAX_ATTEMPTS", 10)
    login_rate_limit_window_seconds: int = read_int_env("LOGIN_RATE_LIMIT_WINDOW_SECONDS", 60)
    cors_allow_origins: tuple[str, ...] = read_csv_env("CORS_ALLOW_ORIGINS", ("*",))
    # Number of reverse proxies in front of the app. 0 (the default) means the
    # socket address is the client. Behind a proxy the socket address is the
    # *proxy's*, so every login attempt shares one rate-limit key: ten bad
    # passwords from anyone would lock out everybody, and a real attacker is
    # never throttled. Set this to the hop count and X-Forwarded-For is trusted
    # to exactly that depth — never further, because the header is client-
    # supplied and anything beyond your own proxies is forgeable.
    trusted_proxy_count: int = read_int_env("TRUSTED_PROXY_COUNT", 0)
    session_event_history_limit: int = read_int_env("SESSION_EVENT_HISTORY_LIMIT", 500)
    session_sse_enabled: bool = read_bool_env("SESSION_SSE_ENABLED", False)
    # Max events buffered per connected SSE client before the oldest is dropped
    # (bounds memory when a consumer stalls). 0 disables the bound.
    sse_client_queue_maxsize: int = read_int_env("SSE_CLIENT_QUEUE_MAXSIZE", 1000)
    # Max number of per-session event states retained in memory; idle (no-client)
    # states are evicted once this is exceeded.
    sse_max_tracked_sessions: int = read_int_env("SSE_MAX_TRACKED_SESSIONS", 1000)
    session_name_max_length: int = 80
    storage_backend: str = os.getenv("STORAGE_BACKEND", "local").strip().lower() or "local"
    object_prefix: str = os.getenv("OBJECT_PREFIX", "").strip().strip("/")
    # --- Google Cloud Storage backend (STORAGE_BACKEND=gcs) -----------------
    # Bucket that holds every uploaded source object. Required for the GCS
    # backend; the factory refuses to build one without it rather than failing
    # later on the first upload.
    gcs_bucket: str = os.getenv("GCS_BUCKET", "").strip()
    gcs_project: str = os.getenv("GCS_PROJECT", "").strip()
    # Origin allowed to PUT directly at a resumable session URI. Browsers are
    # blocked by CORS without it, so it must match the site serving the SPA.
    gcs_upload_origin: str = os.getenv("GCS_UPLOAD_ORIGIN", "").strip()
    # Lifetime of the V4 signed URLs handed out for playback/download.
    gcs_signed_url_ttl_seconds: int = read_int_env("GCS_SIGNED_URL_TTL_SECONDS", 3600)
    # Where a worker keeps its local copy of a bucket object. ffmpeg, WhisperX
    # and the scorers are all path-based, so every remote source is materialised
    # here once and reused across jobs and retries.
    gcs_cache_root_override: str = os.getenv("GCS_CACHE_ROOT", "").strip()
    upload_part_size_mb: int = read_int_env("UPLOAD_PART_SIZE_MB", 8)
    upload_session_ttl_hours: int = read_int_env("UPLOAD_SESSION_TTL_HOURS", 24)
    job_queue_backend: str = os.getenv("JOB_QUEUE_BACKEND", "local").strip().lower() or "local"
    local_job_auto_start: bool = read_bool_env("LOCAL_JOB_AUTO_START", True)
    job_worker_concurrency: int = read_int_env("JOB_WORKER_CONCURRENCY", 2)
    # How many jobs may hold the accelerator at once. Concurrency above bounds
    # jobs, which are mostly network-bound and cheap to overlap; this bounds the
    # one step that is not. Transcription and person detection each load a
    # model into the same card, so with one GPU this stays at 1 and the second
    # job waits for the lease instead of dying of CUDA OOM. 0 = unbounded.
    gpu_slots: int = read_int_env("GPU_SLOTS", 1)
    recover_running_jobs_on_startup: bool = read_bool_env("RECOVER_RUNNING_JOBS_ON_STARTUP", True)
    app_database_url: str = os.getenv("APP_DATABASE_URL", "").strip()
    database_url: str = os.getenv("DATABASE_URL", "").strip()
    # Run "alembic upgrade head" during startup so a single command still brings
    # the app up on a fresh machine. Set false where migrations are applied
    # deliberately as a deploy step (`cd fastapi_backend && alembic upgrade
    # head`) — notably when several processes boot at once and only one of them
    # should be touching the schema.
    db_auto_migrate: bool = read_bool_env("DB_AUTO_MIGRATE", True)
    hatchet_worker_name: str = os.getenv("HATCHET_WORKER_NAME", "osce-ai-marker-worker").strip() or "osce-ai-marker-worker"
    hatchet_job_retries: int = read_int_env("HATCHET_JOB_RETRIES", 2)
    hatchet_job_schedule_timeout_minutes: int = read_int_env("HATCHET_JOB_SCHEDULE_TIMEOUT_MINUTES", 60)
    hatchet_job_execution_timeout_minutes: int = read_int_env("HATCHET_JOB_EXECUTION_TIMEOUT_MINUTES", 240)
    # How often the Hatchet worker re-scans for queued jobs that were never
    # dispatched (or whose dispatch went stale) and dispatches them. This is the
    # safety net that makes queueing continuous even if the API process fails to
    # dispatch at enqueue time. 0 disables (startup-only recovery).
    hatchet_redispatch_interval_seconds: int = read_int_env("HATCHET_REDISPATCH_INTERVAL_SECONDS", 30)

    # Watchdog for external commands (ffmpeg, WhisperX, the scorers). Generous
    # by design: it exists to end a *hung* child, not to bound a slow one. A CPU
    # WhisperX run on a long recording can legitimately take hours. 0 disables.
    subprocess_timeout_seconds: int = read_int_env("SUBPROCESS_TIMEOUT_SECONDS", 4 * 60 * 60)

    # --- Read-through cache ---------------------------------------------
    # Serves hot projections (the session index, the notification feed) from
    # memory, and lets a database change announcement evict them. Disable only
    # to isolate a suspected staleness bug: every read then goes to the
    # database, which is correct but materially slower under load.
    cache_enabled: bool = read_bool_env("CACHE_ENABLED", True)

    # --- Outbound notification webhooks ---------------------------------
    # Per-request timeout for one delivery attempt. Kept short: a webhook is a
    # fire-and-forget announcement, not a transaction worth waiting on.
    webhook_timeout_seconds: float = read_float_env("WEBHOOK_TIMEOUT_SECONDS", 10.0)
    # Total attempts per event per subscription (1 = no retry). Only transport
    # errors, 5xx, and 429 are retried; a 4xx is a refusal, not a hiccup.
    webhook_max_attempts: int = read_int_env("WEBHOOK_MAX_ATTEMPTS", 3)
    # Off by default so the safe behaviour needs no configuration: a webhook URL
    # resolving to loopback/private space is refused, because the server making
    # a caller-supplied request is a textbook SSRF primitive. Turn on to point a
    # webhook at a listener on your own machine during development.
    webhook_allow_private_urls: bool = read_bool_env("WEBHOOK_ALLOW_PRIVATE_URLS", False)
    webhook_user_agent: str = (
        os.getenv("WEBHOOK_USER_AGENT", "").strip() or "OSCE-AI-Marker-Webhook/1.0"
    )

    ffmpeg_bin: str = ""
    ffprobe_bin: str = ""
    scorer_python_bin: str = ""
    whisperx_bin: str = os.getenv("WHISPERX_BIN", "whisperx").strip() or "whisperx"
    whisperx_language: str = os.getenv("WHISPERX_LANGUAGE", "en")
    # The WhisperX CLI defaults to "small" when --model is omitted, so this must
    # always be passed explicitly. large-v3 is the accuracy default; use
    # distil-large-v3 if transcription latency becomes a problem.
    whisperx_model: str = os.getenv("WHISPERX_MODEL", "large-v3").strip() or "large-v3"
    whisperx_device: str = os.getenv("WHISPERX_DEVICE", "cuda").strip() or "cuda"
    # The WhisperX CLI only accepts default/float16/float32/int8. float16 matches
    # the native large-v3 weight precision (~3 GB) and is the accuracy default;
    # float32 only upcasts the same weights for ~2x the VRAM. Set int8 (~1.5 GB) on
    # <=4 GB cards where float16 plus pyannote diarisation does not fit.
    whisperx_compute_type: str = os.getenv("WHISPERX_COMPUTE_TYPE", "float16").strip() or "float16"
    # WhisperX CLI defaults to 8, which OOMs large-v3 on <=6GB cards once the
    # pyannote diarisation models share the device. Raise if you get a bigger GPU.
    whisperx_batch_size: int = read_int_env("WHISPERX_BATCH_SIZE", 1)
    # An OSCE encounter has a known cast — one student and one simulated
    # patient — so the diarisation clustering is told the count instead of
    # inferring it. Left unconstrained, pyannote routinely splits one person
    # across two labels mid-consultation, which reads to the scorer as the
    # student never having asked the question. Raise the maximum for stations
    # that also record an examiner; 0 on either bound omits that flag and
    # restores WhisperX's own speaker-count estimation.
    whisperx_min_speakers: int = read_int_env("WHISPERX_MIN_SPEAKERS", 2)
    whisperx_max_speakers: int = read_int_env("WHISPERX_MAX_SPEAKERS", 2)
    # WhisperX merges VAD segments up to this many seconds before a single
    # decode. The CLI default of 30 spans four or five speaker turns in a
    # consultation, and every word in the chunk then shares one decode context
    # and one avg_logprob. 20 keeps confidence granularity useful and the texts
    # short enough for the aligner to place. 0 keeps the WhisperX default.
    whisperx_chunk_size: int = read_int_env("WHISPERX_CHUNK_SIZE", 20)
    # --print_progress makes WhisperX emit "Progress: 42.10%..." lines that the
    # pipeline parses into real step progress for the session cards. Turn off
    # to restore quiet output (the elapsed-time heartbeat still runs).
    whisperx_print_progress: bool = read_bool_env("WHISPERX_PRINT_PROGRESS", True)
    whisperx_output_format: str = os.getenv("WHISPERX_OUTPUT_FORMAT", "all").strip() or "all"
    whisperx_log_heartbeat_ms: int = read_int_env("WHISPERX_LOG_HEARTBEAT_MS", 5000)
    # ffmpeg -af chain for the dedicated WhisperX input WAV; "" disables the
    # extra pass and WhisperX reads the extracted MP3 directly.
    whisperx_audio_filters: str = os.getenv("WHISPERX_AUDIO_FILTERS", "highpass=f=80,loudnorm").strip()
    # Optional register-priming sentence passed as --initial_prompt ("" = off).
    whisperx_initial_prompt: str = os.getenv("WHISPERX_INITIAL_PROMPT", "").strip()
    transcript_correction_min_ratio: float = read_float_env("TRANSCRIPT_CORRECTION_MIN_RATIO", 0.84)
    # Phonetic (Double Metaphone) matching channel. An ASR model does not
    # misspell a drug name by a character or two — it emits ordinary words that
    # sound like it ("para set a mole"), which no edit-distance threshold can
    # reach. The phonetic channel matches on pronunciation and across word
    # boundaries; these bounds keep it from swapping merely similar-sounding
    # vocabulary.
    transcript_correction_phonetic: bool = read_bool_env("TRANSCRIPT_CORRECTION_PHONETIC", True)
    transcript_correction_min_phonetic_ratio: float = read_float_env(
        "TRANSCRIPT_CORRECTION_MIN_PHONETIC_RATIO", 0.90
    )
    transcript_correction_min_phonetic_char_ratio: float = read_float_env(
        "TRANSCRIPT_CORRECTION_MIN_PHONETIC_CHAR_RATIO", 0.5
    )
    transcript_correction_max_extra_span_words: int = read_int_env(
        "TRANSCRIPT_CORRECTION_MAX_EXTRA_SPAN_WORDS", 3
    )

    # --- hallucination screening ----------------------------------------
    # The WhisperX CLI is invoked without --logprob_threshold /
    # --compression_ratio_threshold, and Canary-Qwen has no equivalent, so the
    # classic Whisper hallucination signature is checked after normalization
    # instead. Flagging is the default; dropping removes the segment before any
    # scorer reads it, which matters most for the communication branch, where a
    # hallucinated "I understand, that must be difficult for you" reads as
    # empathy the student never showed. Either way every hit is recorded in the
    # transcript's "hallucinations" block.
    transcript_hallucination_filter: bool = read_bool_env("TRANSCRIPT_HALLUCINATION_FILTER", True)
    transcript_hallucination_drop: bool = read_bool_env("TRANSCRIPT_HALLUCINATION_DROP", False)
    transcript_hallucination_min_avg_logprob: float = read_float_env(
        "TRANSCRIPT_HALLUCINATION_MIN_AVG_LOGPROB", -1.0
    )
    transcript_hallucination_max_compression_ratio: float = read_float_env(
        "TRANSCRIPT_HALLUCINATION_MAX_COMPRESSION_RATIO", 2.4
    )
    transcript_hallucination_max_ngram_repeats: int = read_int_env(
        "TRANSCRIPT_HALLUCINATION_MAX_NGRAM_REPEATS", 2
    )

    # --- transcription engine selection ---------------------------------
    # Deployment-wide default engine. The settings screen stores an operator
    # selection in the database that overrides this per run; this value is the
    # fallback when nothing is stored, and when a stored selection names an
    # engine this build no longer ships.
    transcription_engine: str = os.getenv("TRANSCRIPTION_ENGINE", "whisperx").strip() or "whisperx"
    # Download the selected engine's weights at startup, in the background, so
    # the first assessment of a deployment does not stall behind a multi-
    # gigabyte fetch. Turn off for an air-gapped or metered host: the engine
    # still downloads on demand when it is first run.
    transcription_prefetch_models: bool = read_bool_env("TRANSCRIPTION_PREFETCH_MODELS", True)

    # --- Canary-Qwen (NVIDIA NeMo SALM) ---------------------------------
    canary_model: str = os.getenv("CANARY_MODEL", "nvidia/canary-qwen-2.5b").strip() or "nvidia/canary-qwen-2.5b"
    # The model was trained on segments of at most 40 s; longer chunks degrade
    # accuracy, so long recordings are decoded in overlapping windows.
    canary_chunk_seconds: float = read_float_env("CANARY_CHUNK_SECONDS", 30.0)
    canary_overlap_seconds: float = read_float_env("CANARY_OVERLAP_SECONDS", 2.0)
    canary_batch_size: int = read_int_env("CANARY_BATCH_SIZE", 1)
    canary_device: str = os.getenv("CANARY_DEVICE", "auto").strip() or "auto"
    # Canary returns text only. Diarisation is on by default because the
    # scorers read speaker-tagged dialogue.
    canary_diarize: bool = read_bool_env("CANARY_DIARIZE", True)
    canary_prompt: str = os.getenv("CANARY_PROMPT", "Transcribe the following:").strip() or "Transcribe the following:"
    canary_audio_filters: str = os.getenv("CANARY_AUDIO_FILTERS", "highpass=f=80").strip()

    # --- standalone diarisation (engines that cannot label speakers) -----
    diarization_model: str = (
        os.getenv("DIARIZATION_MODEL", "pyannote/speaker-diarization-community-1").strip()
        or "pyannote/speaker-diarization-community-1"
    )
    diarization_device: str = os.getenv("DIARIZATION_DEVICE", "auto").strip() or "auto"

    audio_mp3_sample_rate: str = os.getenv("AUDIO_MP3_SAMPLE_RATE", "48000").strip() or "48000"
    audio_mp3_vbr_quality: str = os.getenv("AUDIO_MP3_VBR_QUALITY", "0").strip() or "0"
    enable_scoring: bool = read_bool_env("ENABLE_SCORING", True)
    enable_audio_professionalism: bool = read_bool_env("ENABLE_AUDIO_PROFESSIONALISM", True)
    enable_communication_scoring: bool = read_bool_env("ENABLE_COMMUNICATION_SCORING", True)
    parallel_scoring: bool = read_bool_env("PARALLEL_SCORING", True)

    auto_crop_min_clip_seconds: float = read_float_env("AUTO_CROP_MIN_CLIP_SECONDS", 0.5)
    bell_end_offset_seconds: float = read_float_env("BELL_END_OFFSET_SECONDS", 5.0)
    bell_start_offset_seconds: float = read_float_env("BELL_START_OFFSET_SECONDS", 0.0)
    enable_python_bell_detector: bool = read_bool_env("ENABLE_PYTHON_BELL_DETECTOR", True)
    python_bell_min_gap_seconds: float = read_float_env("PYTHON_BELL_MIN_GAP_SECONDS", 240.0)
    python_bell_min_clips: int = read_int_env("PYTHON_BELL_MIN_CLIPS", 1)
    # 0 (the default) = no upper bound: cohort size is a property of the tape,
    # not something the detector may veto. Set a positive value only to make an
    # obviously runaway detection fail loudly.
    python_bell_max_clips: int = read_int_env("PYTHON_BELL_MAX_CLIPS", 0)
    python_bell_expected_count: int = read_int_env("PYTHON_BELL_EXPECTED_COUNT", 0)
    bell_detector_sample_rate: int = read_int_env("BELL_DETECTOR_SAMPLE_RATE", 22050)
    bell_detector_chunk_seconds: float = read_float_env("BELL_DETECTOR_CHUNK_SECONDS", 60.0)
    python_expected_students: int = read_int_env("PYTHON_EXPECTED_STUDENTS", 0)
    python_min_silence_gap_seconds: float = read_float_env("PYTHON_MIN_SILENCE_GAP_SECONDS", 6.0)

    # Person-presence (RT-DETR) segmentation for the long-video auto-crop
    # workflow. The per-session choice comes from the upload form; these are
    # the server-side enable switch and clip padding. Detector tuning knobs
    # (HUMAN_SEGMENTS_SAMPLE_FPS, HUMAN_SEGMENTS_END_AFTER_SECONDS, ...) are
    # read directly by scripts/detect_human_segments.py from the inherited
    # environment.
    enable_human_detector: bool = read_bool_env("ENABLE_HUMAN_DETECTOR", True)
    human_detector_end_offset_seconds: float = read_float_env("HUMAN_SEGMENTS_END_OFFSET_SECONDS", 2.0)
    human_detector_start_offset_seconds: float = read_float_env("HUMAN_SEGMENTS_START_OFFSET_SECONDS", 0.0)
    # Parallel detector worker processes (each loads its own model copy).
    # Default 1: on a single small GPU CUDA serializes across processes, so
    # extra workers cost 2x VRAM for ~no speedup — opt-in for multi-GPU/CPU.
    human_detector_workers: int = read_int_env("HUMAN_SEGMENTS_WORKERS", 1)

    @property
    def paths(self) -> StoragePaths:
        raw = os.getenv("STORAGE_ROOT", "").strip()
        override = Path(raw).expanduser() if raw else None
        return StoragePaths(root_dir=self.root_dir, storage_root_override=override)

    @property
    def gcs_cache_root(self) -> Path:
        """Local scratch directory holding materialised copies of bucket objects."""
        if self.gcs_cache_root_override:
            return Path(self.gcs_cache_root_override).expanduser()
        return self.paths.storage_root / "cache" / "objects"

    @property
    def frontend_dist_dir(self) -> Path:
        """Directory holding the built frontend (index.html + assets/)."""
        if self.frontend_dist_dir_override:
            return Path(self.frontend_dist_dir_override).expanduser()
        return self.root_dir / "dist"

    @property
    def frontend_index_path(self) -> Path:
        return self.frontend_dist_dir / "index.html"

    @property
    def binds_loopback_only(self) -> bool:
        """True when only this machine can reach the API (the dev default)."""
        return self.api_host.strip().lower() in LOOPBACK_HOSTS

    @property
    def object_storage_root(self) -> Path:
        raw = os.getenv("OBJECT_STORAGE_ROOT", "").strip()
        return Path(raw).expanduser() if raw else self.paths.storage_root / "objects"

    @property
    def upload_part_size_bytes(self) -> int:
        return max(1, self.upload_part_size_mb) * 1024 * 1024

    @property
    def max_video_upload_bytes(self) -> int:
        return max(1, self.max_video_upload_mb) * 1024 * 1024

    @property
    def max_case_study_upload_bytes(self) -> int:
        return max(1, self.max_case_study_upload_mb) * 1024 * 1024

    @property
    def resolved_database_path(self) -> Path:
        raw = self.resolved_app_database_url.strip()
        if not raw:
            return self.paths.database_path
        if raw.startswith("sqlite:///"):
            return Path(raw.removeprefix("sqlite:///")).expanduser()
        if raw.startswith("sqlite://"):
            return Path(raw.removeprefix("sqlite://")).expanduser()
        raise ValueError("resolved_database_path is only available for SQLite URLs; use resolved_database_source.")

    @property
    def resolved_app_database_url(self) -> str:
        return self.app_database_url or self.database_url

    @property
    def resolved_database_source(self) -> Path | str:
        raw = self.resolved_app_database_url.strip()
        if not raw:
            return self.paths.database_path
        if raw.startswith(("postgres://", "postgresql://")):
            return raw
        return self.resolved_database_path

    @property
    def python_bell_pairing_mode(self) -> str:
        raw = os.getenv("PYTHON_BELL_PAIRING_MODE", "pair").strip().lower()
        return "continuous" if raw == "continuous" else "pair"

    @property
    def python_detector_mode(self) -> str:
        raw = os.getenv("PYTHON_DETECTOR_MODE", "hybrid").strip().lower()
        return raw if raw in {"bells", "silence", "hybrid"} else "hybrid"

    @property
    def scorer_script_path(self) -> Path:
        return self.root_dir / "scripts" / "nvidia_osce_assessor.py"

    @property
    def panel_adjudicator_script_path(self) -> Path:
        return self.root_dir / "scripts" / "osce_panel_adjudicator.py"

    @property
    def llm_preprocess_script_path(self) -> Path:
        return self.root_dir / "scripts" / "nemotron_transcript_preprocessor.py"

    @property
    def audio_professionalism_script_path(self) -> Path:
        return self.root_dir / "scripts" / "audio_professionalism_extractor.py"

    @property
    def communication_scorer_script_path(self) -> Path:
        return self.root_dir / "scripts" / "nvidia_osce_communication.py"

    @property
    def rubric_parser_script_path(self) -> Path:
        return self.root_dir / "scripts" / "parse_communication_rubric.py"

    @property
    def bell_detector_script_path(self) -> Path:
        return self.root_dir / "scripts" / "detect_bell_segments.py"

    @property
    def human_detector_script_path(self) -> Path:
        return self.root_dir / "scripts" / "detect_human_segments.py"

    @property
    def canary_script_path(self) -> Path:
        return self.root_dir / "scripts" / "canary_qwen_transcribe.py"

    @property
    def diarization_script_path(self) -> Path:
        return self.root_dir / "scripts" / "pyannote_diarize.py"

    @property
    def auto_crop_segmentation_default(self) -> str:
        """Server default segmentation method when the upload did not choose one."""
        raw = os.getenv("AUTO_CROP_SEGMENTATION", "bells").strip().lower()
        return raw if raw in {"bells", "person"} else "bells"

    def media_tool_path_env(self) -> dict[str, str]:
        """PATH override putting the resolved ffmpeg/ffprobe directories first.

        ``Settings.load`` resolves those binaries to absolute paths (a winget
        package directory, a chocolatey shim) that are routinely *not* on PATH.
        Every command this backend launches itself is given the absolute path,
        but the children launched by those children are not:

        * WhisperX's ``load_audio`` spawns a bare ``ffmpeg`` and dies with
          ``FileNotFoundError: [WinError 2]`` when it is not on PATH;
        * torchcodec (loaded by pyannote) links FFmpeg's shared libraries at
          import time and degrades to "Could not load libtorchcodec";
        * librosa's audioread fallback shells out the same way.

        Handing those processes an augmented PATH keeps the resolution rule in
        one place instead of teaching each third-party tool a new setting.
        Returns an empty mapping when the binaries are bare command names, which
        means they were found on PATH already.
        """
        tool_dirs: list[str] = []
        for binary in (self.ffmpeg_bin, self.ffprobe_bin):
            value = str(binary or "").strip()
            if not value or not any(separator in value for separator in ("/", "\\")):
                continue
            parent = str(Path(value).expanduser().parent)
            if parent and parent not in tool_dirs:
                tool_dirs.append(parent)
        if not tool_dirs:
            return {}
        inherited = os.environ.get("PATH", "")
        return {"PATH": os.pathsep.join([*tool_dirs, inherited] if inherited else tool_dirs)}

    def subprocess_env(self, extra_env: dict[str, str] | None = None) -> dict[str, str]:
        """Base environment for every child process the pipeline launches.

        Unbuffered UTF-8 output (so streamed logs arrive line by line and
        non-ASCII transcript text does not raise on Windows consoles) plus the
        media-tool PATH. ``extra_env`` wins over both.
        """
        env = {"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        env.update(self.media_tool_path_env())
        env.update(extra_env or {})
        return env

    def collect_runtime_warnings(self) -> list[str]:
        """Return human-readable warnings for insecure/permissive configuration.

        Emitted (not raised) at startup so operators are alerted without breaking
        local development defaults.
        """
        warnings: list[str] = []
        if "*" in self.cors_allow_origins:
            warnings.append(
                "CORS_ALLOW_ORIGINS allows any origin ('*'); set explicit origins "
                "before any shared/production deployment."
            )
        if not self.protect_media_endpoints:
            warnings.append(
                "PROTECT_MEDIA_ENDPOINTS is disabled; /media artifacts (videos, "
                "PDFs, scores) are served without authentication."
            )
        warnings.extend(self._account_warnings())
        warnings.extend(self._frontend_warnings())
        warnings.extend(self._human_detector_warnings())
        return warnings

    def _frontend_warnings(self) -> list[str]:
        """Warn when the built frontend is asked for but is not there.

        Not a refusal: the API is complete without it (a reverse proxy may be
        serving dist/ instead). But an operator who set SERVE_FRONTEND=true and
        forgot ``npm run build`` should read why the root URL answers 404 at
        boot, not in the browser.
        """
        if not self.serve_frontend:
            return []
        if self.frontend_index_path.is_file():
            return []
        return [
            f"SERVE_FRONTEND is enabled but no build was found at {self.frontend_dist_dir} "
            "(no index.html). Run 'npm run build' or point FRONTEND_DIST_DIR at the build; "
            "until then the root URL answers 404 and only /api and /media are served."
        ]

    def _account_warnings(self) -> list[str]:
        """Warn when invitations and password resets cannot actually reach anyone.

        Neither condition breaks a boot — an admin can still copy an invitation
        link out of the screen — but a deployment that expects emails to go out
        should learn at startup, not from a colleague who never got one.
        """
        warnings: list[str] = []
        if self.email_backend == "console":
            warnings.append(
                "EMAIL_BACKEND=console: invitation and password-reset emails are written to "
                "the server log instead of being sent. Set EMAIL_BACKEND=smtp with SMTP_HOST "
                "and EMAIL_FROM before inviting markers by email."
            )
        if not self.app_public_url.lower().startswith("https://") and "localhost" not in self.app_public_url:
            warnings.append(
                f"APP_PUBLIC_URL ({self.app_public_url}) is not https; the links in invitation "
                "and password-reset emails will be sent over plaintext."
            )
        return warnings

    def _human_detector_warnings(self) -> list[str]:
        """Warn at startup when person segmentation is enabled but cannot run.

        The RT-DETR detector runs in a subprocess of the SAME venv, so missing
        pieces are detectable here without loading the model. Emitting these at
        startup means an operator learns about a broken vision stack before the
        first user selects 'Human detection' in the UI and hits a failed job.
        """
        if not self.enable_human_detector:
            return []
        warnings: list[str] = []
        if not self.human_detector_script_path.exists():
            warnings.append(
                f"Human-detection segmentation is enabled but the detector script "
                f"is missing at {self.human_detector_script_path}. Long uploads "
                "selecting 'Human detection' will fall back to bell detection."
            )
            return warnings
        import importlib.util

        missing = [name for name in ("torch", "transformers") if importlib.util.find_spec(name) is None]
        if missing:
            warnings.append(
                "Human-detection segmentation is enabled but its dependencies are "
                f"missing ({', '.join(missing)}). Install them with 'uv sync' "
                "or set ENABLE_HUMAN_DETECTOR=false; long uploads "
                "selecting 'Human detection' will fall back to bell detection."
            )
        return warnings

    @classmethod
    def load(cls) -> "Settings":
        raw_root = os.getenv("APP_ROOT", "").strip()
        root_dir = Path(raw_root).expanduser() if raw_root else Path(__file__).resolve().parents[3]
        # winget installs ffmpeg under a versioned package directory and does not
        # always create shims in WinGet\Links, so the bin folder stays off PATH.
        # Newest version first, so an upgrade is picked up without touching .env.
        winget_ffmpeg_bins = sorted(
            (
                bin_dir
                for pattern in ("Gyan.FFmpeg*", "BtbN.FFmpeg*")
                for bin_dir in winget_packages_dir().glob(f"{pattern}/*/bin")
            ),
            reverse=True,
        )
        windows_ffmpeg = [
            Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
            Path(r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"),
            Path(r"C:\Program Files (x86)\ffmpeg\bin\ffmpeg.exe"),
            Path(r"C:\ProgramData\chocolatey\bin\ffmpeg.exe"),
            *(bin_dir / "ffmpeg.exe" for bin_dir in winget_ffmpeg_bins),
        ]
        windows_ffprobe = [
            Path(r"C:\ffmpeg\bin\ffprobe.exe"),
            Path(r"C:\Program Files\ffmpeg\bin\ffprobe.exe"),
            Path(r"C:\Program Files (x86)\ffmpeg\bin\ffprobe.exe"),
            Path(r"C:\ProgramData\chocolatey\bin\ffprobe.exe"),
            *(bin_dir / "ffprobe.exe" for bin_dir in winget_ffmpeg_bins),
        ]
        python_candidates = (
            [root_dir / ".venv" / "Scripts" / "python.exe"]
            if os.name == "nt"
            else [root_dir / ".venv" / "bin" / "python"]
        )
        whisperx_candidates = (
            [root_dir / ".venv" / "Scripts" / "whisperx.exe"]
            if os.name == "nt"
            else [root_dir / ".venv" / "bin" / "whisperx"]
        )
        return cls(
            root_dir=root_dir,
            ffmpeg_bin=resolve_binary_from_candidates(
                os.getenv("FFMPEG_BIN"),
                "ffmpeg",
                windows_ffmpeg if os.name == "nt" else [],
            ),
            ffprobe_bin=resolve_binary_from_candidates(
                os.getenv("FFPROBE_BIN"),
                "ffprobe",
                windows_ffprobe if os.name == "nt" else [],
            ),
            scorer_python_bin=resolve_binary_from_candidates(
                os.getenv("SCORER_PYTHON_BIN"),
                "python",
                python_candidates,
            ),
            whisperx_bin=resolve_binary_from_candidates(
                os.getenv("WHISPERX_BIN"),
                "whisperx",
                whisperx_candidates,
            ),
        )


settings = Settings.load()
