from __future__ import annotations

import logging
from dataclasses import dataclass

from app.core.config import Settings
from app.core.logging_utils import log_context
from app.core.process import CommandRunner
from app.core.rate_limit import FixedWindowRateLimiter
from app.database import Database
from app.database.orm import OrmDatabase
from app.pipeline.media import MediaPipeline
from app.pipeline.scoring import ScoringPipeline
from app.repositories.assessment_repository import AssessmentRepository
from app.repositories.job_repository import JobRepository
from app.repositories.rubric_asset_repository import RubricAssetRepository
from app.repositories.session_repository import SessionRepository
from app.repositories.upload_repository import UploadRepository
from app.repositories.video_repository import VideoRepository
from app.services.assessment_service import AssessmentService
from app.services.async_upload_service import AsyncUploadService
from app.services.artifact_service import ArtifactService
from app.services.auth_service import AuthService
from app.services.clip_service import ClipService
from app.services.event_service import EventService
from app.services.job_queue_service import JobQueueService
from app.services.pipeline_service import PipelineService
from app.services.rubric_asset_service import RubricAssetService
from app.services.rubric_service import RubricService
from app.services.session_service import SessionService
from app.services.storage_service import LocalObjectStorageService, create_storage_service


logger = logging.getLogger(__name__)


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
    storage: LocalObjectStorageService
    jobs: JobQueueService
    rubric_assets: RubricAssetService
    assessments: AssessmentService
    rubrics: RubricService
    async_uploads: AsyncUploadService
    media: MediaPipeline
    scoring: ScoringPipeline
    pipeline: PipelineService
    clips: ClipService
    login_rate_limiter: FixedWindowRateLimiter

    async def startup(self, *, dispatch_queued_jobs: bool = True, recover_interrupted_jobs: bool | None = None) -> None:
        should_recover = (
            self.settings.recover_running_jobs_on_startup and self.settings.job_queue_backend == "local"
            if recover_interrupted_jobs is None
            else recover_interrupted_jobs
        )
        for warning in self.settings.collect_runtime_warnings():
            logger.warning("Configuration warning: %s", warning, extra=log_context("startup", "config_validation"))
        await self.artifacts.ensure_storage_layout()
        await self.storage.ensure_layout()
        await self.database.initialize()
        await self.orm_database.initialize()
        await self.sessions.migrate_legacy_sessions()
        await self.auth.initialize()
        await self.rubrics.ensure_parsed()
        # Recover uploads/sessions stuck in "assembling" before dispatching
        # queued jobs, so no job is started for a session in a bad state.
        await self.async_uploads.recover_stale_assembling_uploads()
        await self.jobs.startup(dispatch_queued=dispatch_queued_jobs, recover_interrupted=should_recover)

    async def shutdown(self) -> None:
        await self.jobs.shutdown()
        await self.orm_database.shutdown()


def create_container(settings: Settings | None = None) -> AppContainer:
    active_settings = settings or Settings.load()
    database = Database(active_settings.resolved_database_source)
    orm_database = OrmDatabase(active_settings.resolved_database_source)
    runner = CommandRunner(active_settings.root_dir)
    artifacts = ArtifactService(active_settings)
    auth = AuthService(active_settings)
    events = EventService(active_settings)
    sessions = SessionService(
        active_settings,
        SessionRepository(orm_database, legacy_sessions_dir=active_settings.paths.sessions_dir),
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
    media = MediaPipeline(active_settings, runner, events, auth)
    scoring = ScoringPipeline(active_settings, runner, events, auth, rubrics)
    pipeline = PipelineService(sessions, events, media, scoring, assessments)
    clips = ClipService(sessions, events, media, pipeline, jobs)
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
        VideoRepository(orm_database),
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
        scoring=scoring,
        pipeline=pipeline,
        clips=clips,
        login_rate_limiter=login_rate_limiter,
    )
