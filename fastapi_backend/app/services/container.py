from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum

from app.core.config import Settings
from app.core.logging_utils import log_context
from app.core.process import CommandRunner
from app.core.rate_limit import FixedWindowRateLimiter
from app.core.resources import ResourceLease
from app.core.tasks import BackgroundTaskRegistry
from app.core.versioned_cache import VersionedCache
from app.database import Database
from app.database.change_tracking import install_change_tracking
from app.database.migration_runner import run_database_migrations
from app.database.migrations import apply_additive_migrations
from app.database.orm import OrmDatabase
from app.pipeline.llm_preprocess import TranscriptPreprocessor
from app.pipeline.media import MediaPipeline
from app.pipeline.transcription.registry import EngineDependencies
from app.pipeline.scoring import ScoringPipeline
from app.repositories.app_settings_repository import AppSettingsRepository
from app.services.custom_provider_service import CustomProviderService
from app.services.llm_settings_service import LLMSettingsService
from app.services.provider_credential_service import ProviderCredentialService
from app.repositories.assessment_repository import AssessmentRepository
from app.repositories.corpus_repository import CorpusRepository
from app.repositories.custom_provider_repository import CustomProviderRepository
from app.repositories.job_repository import JobRepository
from app.repositories.notification_repository import NotificationRepository
from app.repositories.provider_credential_repository import ProviderCredentialRepository
from app.repositories.rubric_asset_repository import RubricAssetRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.webhook_repository import WebhookRepository
from app.repositories.upload_repository import UploadRepository
from app.repositories.video_repository import VideoRepository
from app.services.assessment_service import AssessmentService
from app.services.change_feed_service import ChangeFeedService
from app.services.notification_service import NotificationService
from app.services.webhook_dispatcher import WebhookDispatcher
from app.services.async_upload_service import AsyncUploadService
from app.services.artifact_service import ArtifactService
from app.services.auth_service import AuthService
from app.services.clip_service import ClipService
from app.services.event_service import EventService
from app.services.job_queue_service import JobQueueService
from app.services.pipeline_service import PipelineService
from app.services.rubric_asset_service import RubricAssetService
from app.services.rubric_service import RubricService
from app.services.session_maintenance_service import SessionMaintenanceService
from app.services.session_service import SessionService
from app.storage import ObjectStorage, create_storage_service
from app.services.transcription_router import TranscriptionRouter


logger = logging.getLogger(__name__)


class ContainerRole(StrEnum):
    """Which process this container is booting in.

    The same wiring serves two processes with different duties. The API owns
    the schema, the seed data and the startup sweeps that decide what a
    restart interrupted; a Hatchet worker only runs jobs. Running the API's
    sweeps in a worker is not merely wasteful: ``recover_stale_assembling_uploads``
    reasons "an upload still assembling at boot was killed by the restart",
    which is true for the process that assembles and false for a worker booting
    beside a live API that is assembling right now.
    """

    API = "api"
    WORKER = "worker"


@dataclass
class AppContainer:
    settings: Settings
    database: Database
    orm_database: OrmDatabase
    runner: CommandRunner
    artifacts: ArtifactService
    auth: AuthService
    events: EventService
    sessions: SessionService
    storage: ObjectStorage
    jobs: JobQueueService
    rubric_assets: RubricAssetService
    assessments: AssessmentService
    rubrics: RubricService
    async_uploads: AsyncUploadService
    media: MediaPipeline
    transcription: TranscriptionRouter
    scoring: ScoringPipeline
    pipeline: PipelineService
    clips: ClipService
    notifications: NotificationService
    webhooks: WebhookRepository
    webhook_dispatcher: WebhookDispatcher
    corpora: CorpusRepository
    app_settings: AppSettingsRepository
    llm_settings: LLMSettingsService
    provider_credentials: ProviderCredentialService
    custom_providers: CustomProviderService
    videos: VideoRepository
    session_maintenance: SessionMaintenanceService
    login_rate_limiter: FixedWindowRateLimiter
    changes: ChangeFeedService
    read_cache: VersionedCache
    # The one accelerator lease every GPU step in this process contends on.
    gpu: ResourceLease
    # Startup work that must not hold the boot: currently the transcription
    # weight prefetch, which can run for minutes on a cold machine.
    background: BackgroundTaskRegistry = field(default_factory=BackgroundTaskRegistry)

    def runs_jobs(self, role: ContainerRole) -> bool:
        """Whether this process executes job handlers (and so needs model weights)."""
        return role is ContainerRole.WORKER or self.settings.job_queue_backend == "local"

    async def startup(
        self,
        *,
        role: ContainerRole = ContainerRole.API,
        dispatch_queued_jobs: bool | None = None,
        recover_interrupted_jobs: bool | None = None,
    ) -> None:
        """Bring the container up for ``role``.

        API only: schema migration, seed data, rubric parse, the upload
        recovery sweeps and startup job recovery. The process that accepts
        uploads is the only one that can tell an interrupted assembly from a
        live one, and the only one that should touch the schema.

        Both: storage layout, table initialisation, change tracking, auth, and
        the job queue's own startup (a worker starts it without recovering or
        dispatching, since Hatchet drives its jobs).

        Whichever role executes jobs prefetches the transcription weights.
        """
        api = role is ContainerRole.API
        if dispatch_queued_jobs is None:
            dispatch_queued_jobs = api
        if recover_interrupted_jobs is None:
            recover_interrupted_jobs = (
                api
                and self.settings.recover_running_jobs_on_startup
                and self.settings.job_queue_backend == "local"
            )
        for warning in self.settings.collect_runtime_warnings():
            logger.warning("Configuration warning: %s", warning, extra=log_context("startup", "config_validation"))
        await self.artifacts.ensure_storage_layout()
        await self.storage.ensure_layout()
        # Alembic owns the schema: it creates it, upgrades it, and adopts a
        # database built by the older create_all path. Everything below is then
        # a no-op on a migrated database, and kept because it is what still
        # builds the schema when DB_AUTO_MIGRATE is off or Alembic is absent.
        # One process migrates; a worker booting alongside must not race it.
        if api and self.settings.db_auto_migrate:
            await run_database_migrations(self.settings.resolved_database_source)
        await self.database.initialize()
        await self.orm_database.initialize()
        # create_all adds missing tables but never alters an existing one, so
        # columns introduced after a database was created need this pass.
        await apply_additive_migrations(self.orm_database.engine)
        # Triggers span both schema layers (sessions/assessment_results from the
        # ORM metadata, jobs from the raw-SQL schema), so they can only be
        # attached once both initialisers have run.
        push_enabled = await install_change_tracking(self.orm_database.engine)
        await self.changes.start(push_enabled=push_enabled)
        await self.auth.initialize()
        if api:
            await self.sessions.migrate_legacy_sessions()
            await self.corpora.seed_defaults()
            await self.rubrics.ensure_parsed()
            # Resume uploads whose assembly a restart cut short, and fail the
            # ones that cannot be resumed, before dispatching queued jobs so no
            # job starts for a session in a bad state.
            await self.async_uploads.recover_stale_assembling_uploads()
            # Reclaim uploads abandoned mid-transfer (parts on disk, session
            # pinned at waiting_for_upload) once their TTL has elapsed. Frees
            # leaked bytes and clears dead session cards.
            await self.async_uploads.recover_expired_uploads()
        await self.jobs.startup(dispatch_queued=dispatch_queued_jobs, recover_interrupted=recover_interrupted_jobs)
        # Weights are fetched after the process is otherwise ready, never
        # before: a deployment must serve requests while a multi-gigabyte
        # checkpoint downloads, and the download is optional for correctness.
        # Only a process that will transcribe needs them.
        if self.runs_jobs(role):
            self.background.spawn(
                self.transcription.prefetch_selected_engine(),
                name="transcription-model-prefetch",
            )

    async def shutdown(self) -> None:
        # Cancelled rather than drained: a half-finished weight download is
        # resumed by the HuggingFace cache on the next boot, and waiting for
        # one would hang shutdown for minutes.
        await self.background.cancel_all()
        await self.jobs.shutdown()
        # Before the change feed stops and the engine is disposed: in-flight
        # webhook deliveries still need to write their delivery-log rows.
        await self.notifications.drain()
        await self.changes.stop()
        await self.orm_database.shutdown()
        # Releases the raw-SQL layer's PostgreSQL pool; a no-op on SQLite.
        self.database.close()


def create_container(settings: Settings | None = None) -> AppContainer:
    active_settings = settings or Settings.load()
    database = Database(active_settings.resolved_database_source)
    orm_database = OrmDatabase(active_settings.resolved_database_source)
    runner = CommandRunner(
        active_settings.root_dir,
        default_timeout_seconds=active_settings.subprocess_timeout_seconds,
    )
    artifacts = ArtifactService(active_settings)
    auth = AuthService(active_settings)
    events = EventService(active_settings)
    changes = ChangeFeedService(orm_database)
    read_cache = VersionedCache(enabled=active_settings.cache_enabled)
    # The database announces a committed write; this hook turns that
    # announcement into an eviction, so the cache is corrected by the write
    # itself rather than by anyone polling to find out. Registered at
    # construction (not startup) so a container built for tests behaves the
    # same as one built by the app.
    changes.add_change_observer(
        lambda table, _version: read_cache.invalidate_tables((table,))
    )
    sessions = SessionService(
        active_settings,
        SessionRepository(orm_database, legacy_sessions_dir=active_settings.paths.sessions_dir),
        cache=read_cache,
        changes=changes,
    )
    storage = create_storage_service(active_settings)
    jobs = JobQueueService(
        active_settings,
        JobRepository(database, active_settings.paths.jobs_dir),
        events,
        sessions,
        storage,
    )
    rubric_assets = RubricAssetService(RubricAssetRepository(orm_database))
    assessments = AssessmentService(AssessmentRepository(orm_database))
    rubrics = RubricService(active_settings, runner, artifacts, rubric_assets)
    # One lease per process: transcription (any engine) and person detection
    # both load a model into the same card and must take turns on it.
    gpu = ResourceLease(active_settings.gpu_slots, "gpu")
    media = MediaPipeline(active_settings, runner, events, auth, gpu=gpu)
    # The change feed is what lets this cache the per-run selections instead of
    # re-querying them: a settings write anywhere fires the table's trigger, and
    # the announcement evicts every process's copy.
    app_settings = AppSettingsRepository(orm_database, changes=changes)
    # The NVIDIA key can arrive from the platform secrets file rather than
    # os.environ, and AuthService only reads it during startup() — after this
    # container is built. Passing a callable defers the lookup to call time.
    # Operator-managed API keys, encrypted with a key derived from the auth
    # secret unless CREDENTIAL_ENCRYPTION_KEY says otherwise. The secret is read
    # through a callable for the same reason the NVIDIA override is: AuthService
    # only loads it during startup(), after this container exists.
    provider_credentials = ProviderCredentialService(
        ProviderCredentialRepository(orm_database),
        master_key_source=lambda: auth.runtime.auth_secret,
        env_key=active_settings.credential_encryption_key,
        # The change feed is what makes caching a credential safe: a rotation
        # anywhere fires a trigger, and the announcement evicts this process's
        # decrypted copy. Without it the service refuses to cache at all rather
        # than risk sending a revoked key.
        changes=changes,
    )
    # Providers an operator defined rather than ones this build ships. Cached
    # on the same terms as the credentials for the same reason: the catalogue is
    # resolved before every scoring run in every process, and an edit has to
    # reach the Hatchet worker without a restart.
    custom_providers = CustomProviderService(
        CustomProviderRepository(orm_database),
        changes=changes,
    )
    llm_settings = LLMSettingsService(
        app_settings,
        key_overrides=lambda: {"nvidia": auth.runtime.nvidia_api_key},
        credential_store=provider_credentials,
        custom_providers=custom_providers,
    )
    scoring = ScoringPipeline(active_settings, runner, events, auth, rubrics, llm_settings=llm_settings)
    webhooks = WebhookRepository(orm_database)
    webhook_dispatcher = WebhookDispatcher(active_settings, webhooks)
    notifications = NotificationService(
        NotificationRepository(orm_database),
        changes=changes,
        webhooks=webhook_dispatcher,
        cache=read_cache,
    )
    corpora = CorpusRepository(orm_database)
    preprocessor = TranscriptPreprocessor(active_settings, runner, events, auth, llm_settings=llm_settings)
    transcription = TranscriptionRouter(
        active_settings,
        events,
        EngineDependencies(active_settings, runner, events, auth, media),
        app_settings=app_settings,
        gpu=gpu,
    )
    pipeline = PipelineService(
        sessions,
        events,
        media,
        scoring,
        assessments,
        notifications,
        preprocessor=preprocessor,
        app_settings=app_settings,
        transcription=transcription,
    )
    videos = VideoRepository(orm_database)
    # Built before ClipService because a repeat assessment of a clip re-runs
    # the child session that already exists rather than creating a second one,
    # and re-running a session is this service's job — not a second copy of it
    # inside the clip path. The dependency is one-way: maintenance knows
    # nothing about clips.
    session_maintenance = SessionMaintenanceService(
        active_settings,
        sessions,
        assessments,
        jobs,
        notifications,
        videos,
    )
    clips = ClipService(sessions, events, media, pipeline, jobs, notifications, maintenance=session_maintenance)
    jobs.bind_handlers(pipeline=pipeline, clips=clips)
    login_rate_limiter = FixedWindowRateLimiter(
        max_attempts=active_settings.login_rate_limit_max_attempts,
        window_seconds=active_settings.login_rate_limit_window_seconds,
    )
    async_uploads = AsyncUploadService(
        active_settings,
        UploadRepository(active_settings.paths.uploads_dir),
        sessions,
        storage,
        jobs,
        media,
        events,
        rubric_assets,
        videos,
        corpora=corpora,
    )
    return AppContainer(
        settings=active_settings,
        database=database,
        orm_database=orm_database,
        runner=runner,
        artifacts=artifacts,
        auth=auth,
        events=events,
        sessions=sessions,
        storage=storage,
        jobs=jobs,
        rubric_assets=rubric_assets,
        assessments=assessments,
        rubrics=rubrics,
        async_uploads=async_uploads,
        media=media,
        transcription=transcription,
        scoring=scoring,
        pipeline=pipeline,
        clips=clips,
        notifications=notifications,
        webhooks=webhooks,
        webhook_dispatcher=webhook_dispatcher,
        corpora=corpora,
        app_settings=app_settings,
        llm_settings=llm_settings,
        provider_credentials=provider_credentials,
        custom_providers=custom_providers,
        videos=videos,
        session_maintenance=session_maintenance,
        login_rate_limiter=login_rate_limiter,
        changes=changes,
        read_cache=read_cache,
        gpu=gpu,
    )
