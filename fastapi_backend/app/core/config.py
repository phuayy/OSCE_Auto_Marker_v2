from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


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


def read_bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    token = str(raw).strip().lower()
    if token in {"1", "true", "yes", "y", "on"}:
        return True
    if token in {"0", "false", "no", "n", "off"}:
        return False
    return default


def read_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def read_float_env(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def read_csv_env(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    values = tuple(item.strip() for item in str(raw).split(",") if item.strip())
    return values or default


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
    api_port: int = read_int_env("API_PORT", 8787)
    max_video_upload_mb: int = read_int_env("MAX_VIDEO_UPLOAD_MB", 2048)
    max_case_study_upload_mb: int = read_int_env("MAX_CASE_STUDY_UPLOAD_MB", 50)
    auth_token_ttl_seconds: int = read_int_env("AUTH_TOKEN_TTL_SECONDS", 60 * 60 * 8)
    default_admin_username: str = os.getenv("DEFAULT_ADMIN_USERNAME", "admin")
    default_admin_password: str = os.getenv("DEFAULT_ADMIN_PASSWORD", "")
    auth_bcrypt_rounds: int = read_int_env("AUTH_BCRYPT_ROUNDS", 12)
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
    session_event_history_limit: int = read_int_env("SESSION_EVENT_HISTORY_LIMIT", 500)
    # Max events buffered per connected SSE client before the oldest is dropped
    # (bounds memory when a consumer stalls). 0 disables the bound.
    sse_client_queue_maxsize: int = read_int_env("SSE_CLIENT_QUEUE_MAXSIZE", 1000)
    # Max number of per-session event states retained in memory; idle (no-client)
    # states are evicted once this is exceeded.
    sse_max_tracked_sessions: int = read_int_env("SSE_MAX_TRACKED_SESSIONS", 1000)
    session_name_max_length: int = 80
    storage_backend: str = os.getenv("STORAGE_BACKEND", "local").strip().lower() or "local"
    object_prefix: str = os.getenv("OBJECT_PREFIX", "").strip().strip("/")
    upload_part_size_mb: int = read_int_env("UPLOAD_PART_SIZE_MB", 8)
    upload_session_ttl_hours: int = read_int_env("UPLOAD_SESSION_TTL_HOURS", 24)
    job_queue_backend: str = os.getenv("JOB_QUEUE_BACKEND", "local").strip().lower() or "local"
    local_job_auto_start: bool = read_bool_env("LOCAL_JOB_AUTO_START", True)
    job_worker_concurrency: int = read_int_env("JOB_WORKER_CONCURRENCY", 2)
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
    whisperx_output_format: str = os.getenv("WHISPERX_OUTPUT_FORMAT", "all").strip() or "all"
    whisperx_log_heartbeat_ms: int = read_int_env("WHISPERX_LOG_HEARTBEAT_MS", 5000)
    # ffmpeg -af chain for the dedicated WhisperX input WAV; "" disables the
    # extra pass and WhisperX reads the extracted MP3 directly.
    whisperx_audio_filters: str = os.getenv("WHISPERX_AUDIO_FILTERS", "highpass=f=80,loudnorm").strip()
    # Optional register-priming sentence passed as --initial_prompt ("" = off).
    whisperx_initial_prompt: str = os.getenv("WHISPERX_INITIAL_PROMPT", "").strip()
    transcript_correction_min_ratio: float = read_float_env("TRANSCRIPT_CORRECTION_MIN_RATIO", 0.84)

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
    python_bell_max_clips: int = read_int_env("PYTHON_BELL_MAX_CLIPS", 16)
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
    def auto_crop_segmentation_default(self) -> str:
        """Server default segmentation method when the upload did not choose one."""
        raw = os.getenv("AUTO_CROP_SEGMENTATION", "bells").strip().lower()
        return raw if raw in {"bells", "person"} else "bells"

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
        warnings.extend(self._human_detector_warnings())
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
                f"missing ({', '.join(missing)}). Install them (pip install torch "
                "transformers) or set ENABLE_HUMAN_DETECTOR=false; long uploads "
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
