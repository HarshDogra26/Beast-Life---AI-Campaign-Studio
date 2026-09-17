"""Per-run dependency container and the stage execution wrapper.
"""

from __future__ import annotations
import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from ..config import Settings
from ..domain.enums import StageName, StageStatus
from ..logging import get_logger, log_context
from ..providers.base import ProviderError
from ..providers.registry import ProviderBundle
from ..providers.retry import with_retry
from ..storage.artifacts import ArtifactStore
from ..storage.db import session_scope
from ..storage.repo import (
    AssetRepository,
    CampaignRepository,
    LedgerRepository,
    ResearchRepository,
    SpecRepository,
    StageRepository,
    fingerprint,
)

log = get_logger(__name__)


class StageSkipped(Exception):  # noqa: N818 — control flow, not an error
    """Raised internally when a stage's cached output is reused."""

    def __init__(self, output: dict | None) -> None:
        super().__init__("stage output reused")
        self.output = output


@dataclass(slots=True)
class RunContext:
    """Everything a node needs, assembled once per run."""

    campaign_id: str
    run_id: str
    settings: Settings
    providers: ProviderBundle
    artifacts: ArtifactStore
    worker_epoch: str

    @property
    def injector(self):
        return self.providers.injector


async def run_stage(
    ctx: RunContext,
    stage: StageName,
    *,
    inputs: Any,
    work: Callable[[], Awaitable[dict | None]],
) -> dict | None:
    """Execute one stage with memoisation, status tracking and typed errors.
    """
    fp = fingerprint(inputs)

    with log_context(campaign_id=ctx.campaign_id, stage=stage.value):
        async with session_scope() as session:
            decision = await StageRepository(session).begin_stage(
                campaign_id=ctx.campaign_id,
                stage=stage,
                input_fingerprint=fp,
                worker_epoch=ctx.worker_epoch,
            )

        if decision.reused:
            async with session_scope() as session:
                await StageRepository(session).set_status(
                    campaign_id=ctx.campaign_id, stage=stage, status=StageStatus.REUSED
                )
            log.info("stage.reused", fingerprint=fp[:12])
            return decision.cached_output

        started = time.monotonic()
        log.info("stage.start", attempt=decision.row.attempt, fingerprint=fp[:12])

        try:
            ctx.injector.maybe_fail(stage.value)
            output = await work()
        except asyncio.CancelledError:
            await _record_failure(
                ctx,
                stage,
                "Cancelled because a concurrent stage failed. "
                "Fix that stage and retry; this one never ran to completion.",
                "cancelled",
                status=StageStatus.INTERRUPTED,
            )
            log.info("stage.cancelled")
            raise
        except ProviderError as exc:
            await _record_failure(ctx, stage, str(exc), exc.kind)
            log.error("stage.failed", kind=exc.kind, error=str(exc)[:300])
            raise
        except Exception as exc:
            await _record_failure(ctx, stage, str(exc), type(exc).__name__)
            log.exception("stage.crashed")
            raise

        async with session_scope() as session:
            await StageRepository(session).complete_stage(
                campaign_id=ctx.campaign_id, stage=stage, output=output
            )
            await CampaignRepository(session).touch(ctx.campaign_id)

        log.info("stage.done", duration_s=round(time.monotonic() - started, 2))
        return output


async def _record_failure(
    ctx: RunContext,
    stage: StageName,
    message: str,
    kind: str | None,
    *,
    status: StageStatus = StageStatus.FAILED,
) -> None:
    async with session_scope() as session:
        repo = StageRepository(session)
        await repo.fail_stage(
            campaign_id=ctx.campaign_id, stage=stage, error=message, error_kind=kind
        )
        if status is not StageStatus.FAILED:
            await repo.set_status(campaign_id=ctx.campaign_id, stage=stage, status=status)


async def retrying(
    ctx: RunContext, label: str, operation: Callable[[], Awaitable[Any]]
) -> Any:
    """Apply the configured retry policy to a provider call.

    Exists so every call site gets the same policy. Retryability is decided by
    the error type, so a transient timeout backs off and retries while a
    content-policy rejection fails immediately — without any call site having to
    know the difference.
    """
    return await with_retry(
        operation,
        attempts=ctx.settings.max_retries,
        base_delay_s=ctx.settings.retry_base_delay_s,
        label=label,
    )


# -- convenience accessors -------------------------------------------------


async def load_spec(campaign_id: str) -> dict | None:
    async with session_scope() as session:
        row = await SpecRepository(session).latest_for_campaign(campaign_id)
        return row.spec if row else None


async def load_research(campaign_id: str) -> dict | None:
    async with session_scope() as session:
        return await ResearchRepository(session).get(campaign_id)


async def save_asset(asset: dict) -> None:
    async with session_scope() as session:
        await AssetRepository(session).save(asset)


async def ledger_dispatch(
    ctx: RunContext, *, call_id: str, stage: str, provider: str, model: str, operation: str
) -> None:
    async with session_scope() as session:
        await LedgerRepository(session).dispatch(
            call_id=call_id,
            campaign_id=ctx.campaign_id,
            stage=stage,
            provider=provider,
            model=model,
            operation=operation,
        )


async def ledger_settle(*, call_id: str, **values: Any) -> None:
    async with session_scope() as session:
        await LedgerRepository(session).settle(call_id=call_id, **values)
