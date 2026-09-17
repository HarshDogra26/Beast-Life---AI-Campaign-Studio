"""Durable background worker.
"""

from __future__ import annotations
import asyncio
import contextlib
import os
import secrets
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from langgraph.errors import GraphInterrupt
from langgraph.types import Command
from ..config import Settings, get_settings
from ..domain.enums import CampaignStatus, JobStatus, StageName, StageStatus
from ..graph.builder import build_graph, checkpointer_context
from ..graph.context import RunContext
from ..graph.state import initial_state
from ..logging import get_logger, log_context
from ..providers.registry import ProviderBundle
from ..storage.artifacts import get_artifact_store
from ..storage.db import session_scope
from ..storage.repo import (
    CampaignRepository,
    JobRepository,
    ResearchRepository,
    StageRepository,
)

log = get_logger(__name__)

JOB_GENERATE = "generate"
JOB_RESUME = "resume_selection"
POLL_INTERVAL_S = 0.4


def new_worker_epoch() -> str:
    """Identifies this process instance. Rotates on every start, by design."""
    return f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"


@dataclass
class WorkerHandle:
    epoch: str
    task: asyncio.Task | None = None
    stopping: asyncio.Event | None = None


class CampaignWorker:
    def __init__(self, *, settings: Settings, providers: ProviderBundle) -> None:
        self._settings = settings
        self._providers = providers
        self.epoch = new_worker_epoch()
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._busy = asyncio.Event()
        self._busy.set()

    # -- lifecycle ---------------------------------------------------------

    async def sweep_orphans(self) -> dict[str, int]:
        """Mark work abandoned by a previous process. Runs once at startup."""
        async with session_scope() as session:
            stages = await StageRepository(session).sweep_orphans(current_epoch=self.epoch)
            jobs = await JobRepository(session).sweep_orphans(current_epoch=self.epoch)

        if stages or jobs:
            await self._mark_interrupted_campaigns()
            log.warning("worker.orphans_swept", stages=stages, jobs=jobs, epoch=self.epoch)
        return {"stages": stages, "jobs": jobs}

    async def _mark_interrupted_campaigns(self) -> None:
        """Move campaigns whose stages were interrupted out of a 'running' status."""
        async with session_scope() as session:
            campaigns = await CampaignRepository(session).list(limit=200)
            stage_repo = StageRepository(session)
            campaign_repo = CampaignRepository(session)
            for campaign in campaigns:
                if campaign.status not in (
                    CampaignStatus.RESEARCHING,
                    CampaignStatus.GENERATING,
                ):
                    continue
                stages = await stage_repo.list(campaign.id)
                if any(s.status == StageStatus.INTERRUPTED for s in stages):
                    await campaign_repo.set_status(
                        campaign.id,
                        CampaignStatus.INTERRUPTED,
                        error="Interrupted by a server restart. Retry the affected stage.",
                    )

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop(), name="campaign-worker")
            log.info("worker.started", epoch=self.epoch)

    async def stop(self, *, timeout_s: float = 20.0) -> None:
        self._stop.set()
        if self._task is None:
            return
        with contextlib.suppress(asyncio.TimeoutError, asyncio.CancelledError):
            await asyncio.wait_for(self._task, timeout=timeout_s)
        if not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
        self._task = None
        log.info("worker.stopped", epoch=self.epoch)

    # -- main loop ---------------------------------------------------------

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                async with session_scope() as session:
                    job = await JobRepository(session).claim_next(worker_epoch=self.epoch)
            except Exception:
                log.exception("worker.claim_failed")
                await asyncio.sleep(POLL_INTERVAL_S * 4)
                continue

            if job is None:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL_S)
                continue

            self._busy.clear()
            try:
                await self._execute(job.id, job.campaign_id, job.kind, dict(job.payload or {}))
            except Exception as exc:
                log.exception("worker.job_crashed", job_id=job.id)
                await self._finish(job.id, JobStatus.FAILED, str(exc))
            finally:
                self._busy.set()

    async def _execute(
        self, job_id: str, campaign_id: str, kind: str, payload: dict[str, Any]
    ) -> None:
        with log_context(campaign_id=campaign_id):
            log.info("job.start", job_id=job_id, kind=kind)
            try:
                if kind == JOB_GENERATE:
                    await self._run_generate(campaign_id, payload)
                elif kind == JOB_RESUME:
                    await self._run_resume(campaign_id, payload)
                else:
                    raise ValueError(f"unknown job kind {kind!r}")
            except Exception as exc:
                await self._fail_campaign(campaign_id, exc)
                await self._finish(job_id, JobStatus.FAILED, str(exc))
                return

            await self._finish(job_id, JobStatus.SUCCEEDED)

    # -- graph runs --------------------------------------------------------

    async def _run_generate(self, campaign_id: str, payload: dict[str, Any]) -> None:
        async with session_scope() as session:
            repo = CampaignRepository(session)
            campaign = await repo.get(campaign_id)
            if campaign is None:
                raise LookupError(f"campaign {campaign_id} not found")
            brief = dict(campaign.brief)
            selected = campaign.selected_angle_id
            thread_id = await repo.start_run(campaign_id)
            await repo.set_status(
                campaign_id,
                CampaignStatus.GENERATING if selected else CampaignStatus.RESEARCHING,
            )

        state = initial_state(
            campaign_id=campaign_id,
            run_id=thread_id,
            brief=brief,
            size_strategy=self._providers.size_strategy.value,
            selected_angle_id=selected,
        )
        if selected:
            async with session_scope() as session:
                report = await ResearchRepository(session).get(campaign_id)
            if report:
                state["angles"] = report.get("angles", [])
                state["research"] = report

        await self._invoke_graph(campaign_id, thread_id, state)

    async def _run_resume(self, campaign_id: str, payload: dict[str, Any]) -> None:
        angle_id = payload.get("angle_id")
        async with session_scope() as session:
            repo = CampaignRepository(session)
            thread_id = await repo.thread_id(campaign_id)
            await repo.set_status(campaign_id, CampaignStatus.GENERATING)
        if not thread_id:
            raise LookupError(f"campaign {campaign_id} has no active run to resume")

        await self._invoke_graph(
            campaign_id, thread_id, Command(resume={"angle_id": angle_id})
        )

    async def _invoke_graph(self, campaign_id: str, thread_id: str, payload: Any) -> None:
        ctx = RunContext(
            campaign_id=campaign_id,
            run_id=thread_id,
            settings=self._settings,
            providers=self._providers,
            artifacts=get_artifact_store(),
            worker_epoch=self.epoch,
        )
        config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 40}

        async with checkpointer_context(self._settings.checkpoint_db) as checkpointer:
            graph = build_graph(ctx, checkpointer)
            try:
                result = await graph.ainvoke(payload, config=config)
            except GraphInterrupt:
                await self._await_selection(campaign_id)
                return

        if self._is_interrupted(result):
            await self._await_selection(campaign_id)
            return

        await self._complete(campaign_id, result)

    @staticmethod
    def _is_interrupted(result: Any) -> bool:
        """LangGraph surfaces a pause as an ``__interrupt__`` key rather than
        raising, depending on how the run terminates — handle both."""
        return isinstance(result, dict) and bool(result.get("__interrupt__"))

    async def _await_selection(self, campaign_id: str) -> None:
        async with session_scope() as session:
            await CampaignRepository(session).set_status(
                campaign_id, CampaignStatus.AWAITING_SELECTION
            )
        log.info("campaign.awaiting_selection")

    async def _complete(self, campaign_id: str, result: dict[str, Any]) -> None:
        async with session_scope() as session:
            stages = await StageRepository(session).list(campaign_id)
            failed = [s.stage for s in stages if s.status == StageStatus.FAILED]
            repo = CampaignRepository(session)
            if failed:
                await repo.set_status(
                    campaign_id,
                    CampaignStatus.FAILED,
                    error=f"stages failed: {', '.join(failed)}",
                )
            else:
                await repo.set_status(campaign_id, CampaignStatus.COMPLETED)
        log.info(
            "campaign.finished",
            assets=sorted((result or {}).get("assets", {})),
            failed_stages=failed,
        )

    async def _fail_campaign(self, campaign_id: str, exc: Exception) -> None:
        async with session_scope() as session:
            await CampaignRepository(session).set_status(
                campaign_id, CampaignStatus.FAILED, error=str(exc)[:2000]
            )
        log.error("campaign.failed", error=str(exc)[:400])

    async def _finish(
        self, job_id: str, status: JobStatus, error: str | None = None
    ) -> None:
        async with session_scope() as session:
            await JobRepository(session).finish(job_id, status=status, error=error)


# --------------------------------------------------------------------------
# enqueue helpers (used by the API layer)
# --------------------------------------------------------------------------


async def enqueue(
    *, campaign_id: str, kind: str, payload: dict[str, Any] | None = None
) -> str:
    """Add a job. Returns its id.
    """
    job_id = f"job_{secrets.token_hex(10)}"
    async with session_scope() as session:
        await JobRepository(session).enqueue(
            job_id=job_id, campaign_id=campaign_id, kind=kind, payload=payload or {}
        )
    log.info("job.enqueued", job_id=job_id, campaign_id=campaign_id, kind=kind)
    return job_id


async def retry_stage(*, campaign_id: str, stage: StageName) -> str:
    """Reset one stage and re-run the graph.
    """
    async with session_scope() as session:
        await StageRepository(session).reset_for_retry(campaign_id=campaign_id, stage=stage)
        await CampaignRepository(session).set_status(
            campaign_id, CampaignStatus.GENERATING, error=None
        )
    return await enqueue(
        campaign_id=campaign_id,
        kind=JOB_GENERATE,
        payload={"retry_stage": stage.value, "requested_at": datetime.now(UTC).isoformat()},
    )


def get_worker() -> CampaignWorker:
    """Resolved from app state in the API layer; see ``app.main``."""
    raise RuntimeError("worker is provided via app.state; use the FastAPI dependency")


def build_worker(providers: ProviderBundle) -> CampaignWorker:
    return CampaignWorker(settings=get_settings(), providers=providers)
