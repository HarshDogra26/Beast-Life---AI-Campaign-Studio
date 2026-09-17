"""Repository layer. All SQL lives here; nothing above it writes a query.
"""

from __future__ import annotations
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from ..domain.enums import CampaignStatus, JobStatus, StageName, StageStatus
from .models import (
    Asset,
    Campaign,
    CampaignSpecRow,
    IdempotencyKey,
    Job,
    ProviderCall,
    ResearchReport,
    Source,
    StageRun,
)


def fingerprint(payload: Any) -> str:
    """Stable sha256 over a JSON-serialisable payload.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class StageDecision:
    """Outcome of asking whether a stage needs to run."""

    should_run: bool
    row: StageRun
    cached_output: dict | None = None

    @property
    def reused(self) -> bool:
        return not self.should_run


class CampaignRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        campaign_id: str,
        title: str,
        brief: dict,
        size_strategy: str,
        image_model: str,
        provider_mode: str,
    ) -> Campaign:
        campaign = Campaign(
            id=campaign_id,
            status=CampaignStatus.DRAFT,
            title=title[:200],
            brief=brief,
            size_strategy=size_strategy,
            image_model=image_model,
            provider_mode=provider_mode,
        )
        self.session.add(campaign)
        await self.session.flush()
        return campaign

    async def get(self, campaign_id: str) -> Campaign | None:
        return await self.session.get(Campaign, campaign_id)

    async def list(self, *, limit: int = 50, offset: int = 0) -> Sequence[Campaign]:
        stmt = (
            select(Campaign)
            .order_by(Campaign.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def count(self) -> int:
        return int((await self.session.execute(select(func.count(Campaign.id)))).scalar_one())

    async def set_status(
        self, campaign_id: str, status: CampaignStatus, *, error: str | None = None
    ) -> None:
        await self.session.execute(
            update(Campaign)
            .where(Campaign.id == campaign_id)
            .values(status=status, error=error, updated_at=_utcnow())
        )

    async def set_selected_angle(self, campaign_id: str, angle_id: str) -> None:
        await self.session.execute(
            update(Campaign)
            .where(Campaign.id == campaign_id)
            .values(selected_angle_id=angle_id, updated_at=_utcnow())
        )

    async def touch(self, campaign_id: str) -> None:
        await self.session.execute(
            update(Campaign).where(Campaign.id == campaign_id).values(updated_at=_utcnow())
        )

    async def start_run(self, campaign_id: str) -> str:
        """Allocate the next run sequence and its checkpoint thread id.
        """
        campaign = await self.session.get(Campaign, campaign_id)
        if campaign is None:
            raise LookupError(f"campaign {campaign_id} not found")
        campaign.run_seq += 1
        campaign.thread_id = f"{campaign_id}:r{campaign.run_seq}"
        campaign.updated_at = _utcnow()
        await self.session.flush()
        return campaign.thread_id

    async def thread_id(self, campaign_id: str) -> str | None:
        campaign = await self.session.get(Campaign, campaign_id)
        return campaign.thread_id if campaign else None


class StageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, campaign_id: str, stage: StageName) -> StageRun | None:
        stmt = select(StageRun).where(
            StageRun.campaign_id == campaign_id, StageRun.stage == stage
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list(self, campaign_id: str) -> Sequence[StageRun]:
        stmt = select(StageRun).where(StageRun.campaign_id == campaign_id)
        rows = (await self.session.execute(stmt)).scalars().all()
        order = {s: i for i, s in enumerate(StageName)}
        return sorted(rows, key=lambda r: order.get(StageName(r.stage), 99))

    async def begin_stage(
        self,
        *,
        campaign_id: str,
        stage: StageName,
        input_fingerprint: str,
        worker_epoch: str,
    ) -> StageDecision:
        """Claim a stage for execution, or report that its output can be reused."""
        row = await self.get(campaign_id, stage)

        if row is None:
            row = StageRun(campaign_id=campaign_id, stage=stage, status=StageStatus.PENDING)
            self.session.add(row)
            await self.session.flush()

        if (
            row.status in (StageStatus.COMPLETED, StageStatus.REUSED)
            and row.input_fingerprint == input_fingerprint
            and row.output is not None
        ):
            return StageDecision(should_run=False, row=row, cached_output=row.output)

        row.status = StageStatus.RUNNING
        row.attempt += 1
        row.input_fingerprint = input_fingerprint
        row.worker_epoch = worker_epoch
        row.started_at = _utcnow()
        row.finished_at = None
        row.error = None
        row.error_kind = None
        await self.session.flush()
        return StageDecision(should_run=True, row=row)

    async def complete_stage(
        self, *, campaign_id: str, stage: StageName, output: dict | None
    ) -> None:
        await self.session.execute(
            update(StageRun)
            .where(StageRun.campaign_id == campaign_id, StageRun.stage == stage)
            .values(
                status=StageStatus.COMPLETED,
                output=output,
                finished_at=_utcnow(),
                error=None,
                error_kind=None,
            )
        )

    async def fail_stage(
        self,
        *,
        campaign_id: str,
        stage: StageName,
        error: str,
        error_kind: str | None = None,
    ) -> None:
        await self.session.execute(
            update(StageRun)
            .where(StageRun.campaign_id == campaign_id, StageRun.stage == stage)
            .values(
                status=StageStatus.FAILED,
                error=error[:4000],
                error_kind=error_kind,
                finished_at=_utcnow(),
            )
        )

    async def set_status(
        self, *, campaign_id: str, stage: StageName, status: StageStatus
    ) -> None:
        await self.session.execute(
            update(StageRun)
            .where(StageRun.campaign_id == campaign_id, StageRun.stage == stage)
            .values(status=status)
        )

    async def reset_for_retry(self, *, campaign_id: str, stage: StageName) -> None:
        """Mark one stage PENDING. Deliberately touches no other row.
        """
        await self.session.execute(
            update(StageRun)
            .where(StageRun.campaign_id == campaign_id, StageRun.stage == stage)
            .values(
                status=StageStatus.PENDING,
                error=None,
                error_kind=None,
                finished_at=None,
                # Clear the fingerprint so begin_stage cannot short-circuit a
                # stage the user explicitly asked to re-run.
                input_fingerprint=None,
            )
        )

    async def sweep_orphans(self, *, current_epoch: str) -> int:
        """Mark RUNNING rows owned by a dead process as INTERRUPTED.
        """
        result = await self.session.execute(
            update(StageRun)
            .where(
                StageRun.status == StageStatus.RUNNING,
                (StageRun.worker_epoch.is_(None)) | (StageRun.worker_epoch != current_epoch),
            )
            .values(
                status=StageStatus.INTERRUPTED,
                error="Process restarted while this stage was running.",
                error_kind="interrupted_by_restart",
                finished_at=_utcnow(),
            )
        )
        return int(result.rowcount or 0)


class ResearchRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save(self, *, campaign_id: str, report: dict) -> None:
        await self.session.execute(
            delete(ResearchReport).where(ResearchReport.campaign_id == campaign_id)
        )
        await self.session.execute(delete(Source).where(Source.campaign_id == campaign_id))
        self.session.add(ResearchReport(campaign_id=campaign_id, report=report))

        for src in report.get("sources", []):
            self.session.add(
                Source(
                    campaign_id=campaign_id,
                    source_id=src["id"],
                    url=str(src["url"]),
                    title=src["title"],
                    accessed_at=_parse_dt(src["accessed_at"]),
                    excerpt=src["excerpt"],
                    summary=src["summary"],
                    injection_flags=src.get("injection_flags", []),
                )
            )
        await self.session.flush()

    async def get(self, campaign_id: str) -> dict | None:
        row = await self.session.get(ResearchReport, campaign_id)
        return row.report if row else None

    async def sources(self, campaign_id: str) -> Sequence[Source]:
        stmt = (
            select(Source)
            .where(Source.campaign_id == campaign_id)
            .order_by(Source.source_id)
        )
        return (await self.session.execute(stmt)).scalars().all()


class SpecRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save(self, *, spec: dict, fingerprint_value: str) -> CampaignSpecRow:
        row = CampaignSpecRow(
            spec_id=spec["spec_id"],
            campaign_id=spec["campaign_id"],
            version=spec.get("version", 1),
            fingerprint=fingerprint_value,
            spec=spec,
        )
        await self.session.merge(row)
        await self.session.flush()
        return row

    async def get(self, spec_id: str) -> CampaignSpecRow | None:
        return await self.session.get(CampaignSpecRow, spec_id)

    async def latest_for_campaign(self, campaign_id: str) -> CampaignSpecRow | None:
        stmt = (
            select(CampaignSpecRow)
            .where(CampaignSpecRow.campaign_id == campaign_id)
            .order_by(CampaignSpecRow.version.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def next_version(self, campaign_id: str) -> int:
        stmt = select(func.max(CampaignSpecRow.version)).where(
            CampaignSpecRow.campaign_id == campaign_id
        )
        current = (await self.session.execute(stmt)).scalar()
        return int(current or 0) + 1


class AssetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def save(self, asset: dict) -> Asset:
        row = Asset(
            id=asset["id"],
            campaign_id=asset["campaign_id"],
            spec_id=asset.get("spec_id"),
            format=asset["format"],
            artifact_path=asset["artifact_path"],
            media_type=asset["media_type"],
            width=asset["width"],
            height=asset["height"],
            byte_size=asset["byte_size"],
            sha256=asset["sha256"],
            duration_s=asset.get("duration_s"),
            request=asset["request"],
        )
        merged = await self.session.merge(row)
        await self.session.flush()
        return merged

    async def get(self, asset_id: str) -> Asset | None:
        return await self.session.get(Asset, asset_id)

    async def list(self, campaign_id: str) -> Sequence[Asset]:
        stmt = (
            select(Asset)
            .where(Asset.campaign_id == campaign_id)
            .order_by(Asset.created_at)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def by_format(self, campaign_id: str, fmt: str) -> Asset | None:
        stmt = (
            select(Asset)
            .where(Asset.campaign_id == campaign_id, Asset.format == fmt)
            .order_by(Asset.created_at.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()


class IdempotencyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def lookup(self, key: str, scope: str) -> dict | None:
        row = await self.session.get(IdempotencyKey, key)
        if row is None:
            return None
        if row.scope != scope:
            raise ValueError(f"Idempotency-Key {key!r} was already used for scope {row.scope!r}")
        return row.response

    async def record(
        self, *, key: str, scope: str, campaign_id: str | None, response: dict
    ) -> None:
        self.session.add(
            IdempotencyKey(key=key, scope=scope, campaign_id=campaign_id, response=response)
        )
        await self.session.flush()


class JobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def enqueue(
        self, *, job_id: str, campaign_id: str, kind: str, payload: dict
    ) -> Job:
        job = Job(id=job_id, campaign_id=campaign_id, kind=kind, payload=payload)
        self.session.add(job)
        await self.session.flush()
        return job

    async def claim_next(self, *, worker_epoch: str) -> Job | None:
        """Take the oldest queued job.

        SQLite serialises writers, so the read-then-update here is safe without an
        explicit lock; a multi-writer database would need SELECT ... FOR UPDATE.
        """
        stmt = (
            select(Job)
            .where(Job.status == JobStatus.QUEUED)
            .order_by(Job.created_at)
            .limit(1)
        )
        job = (await self.session.execute(stmt)).scalar_one_or_none()
        if job is None:
            return None
        job.status = JobStatus.RUNNING
        job.attempts += 1
        job.worker_epoch = worker_epoch
        job.started_at = _utcnow()
        await self.session.flush()
        return job

    async def finish(self, job_id: str, *, status: JobStatus, error: str | None = None) -> None:
        await self.session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(status=status, error=(error or None), finished_at=_utcnow())
        )

    async def get(self, job_id: str) -> Job | None:
        return await self.session.get(Job, job_id)

    async def active_for_campaign(self, campaign_id: str) -> Job | None:
        """An in-flight job for this campaign.
        """
        stmt = (
            select(Job)
            .where(
                Job.campaign_id == campaign_id,
                Job.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]),
            )
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def sweep_orphans(self, *, current_epoch: str) -> int:
        result = await self.session.execute(
            update(Job)
            .where(
                Job.status == JobStatus.RUNNING,
                (Job.worker_epoch.is_(None)) | (Job.worker_epoch != current_epoch),
            )
            .values(
                status=JobStatus.INTERRUPTED,
                error="Worker restarted while this job was running.",
                finished_at=_utcnow(),
            )
        )
        return int(result.rowcount or 0)


class LedgerRepository:
    """Provider usage and estimated cost."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def dispatch(
        self,
        *,
        call_id: str,
        campaign_id: str,
        stage: str,
        provider: str,
        model: str,
        operation: str,
    ) -> None:
        """Record intent to call *before* the network request.
        """
        self.session.add(
            ProviderCall(
                id=call_id,
                campaign_id=campaign_id,
                stage=stage,
                provider=provider,
                model=model,
                operation=operation,
                status="dispatched",
            )
        )
        await self.session.flush()

    async def settle(
        self,
        *,
        call_id: str,
        status: str,
        latency_ms: int,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        image_tokens: int | None = None,
        image_count: int = 0,
        search_credits: int = 0,
        est_cost_usd: float | None = None,
        is_estimate: bool = True,
        error: str | None = None,
    ) -> None:
        await self.session.execute(
            update(ProviderCall)
            .where(ProviderCall.id == call_id)
            .values(
                status=status,
                latency_ms=latency_ms,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                image_tokens=image_tokens,
                image_count=image_count,
                search_credits=search_credits,
                est_cost_usd=est_cost_usd,
                is_estimate=is_estimate,
                error=(error or None),
            )
        )

    async def list(self, campaign_id: str) -> Sequence[ProviderCall]:
        stmt = (
            select(ProviderCall)
            .where(ProviderCall.campaign_id == campaign_id)
            .order_by(ProviderCall.created_at)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def unsettled(self, campaign_id: str) -> Sequence[ProviderCall]:
        """Calls that were dispatched but never settled — possible orphaned spend."""
        stmt = select(ProviderCall).where(
            ProviderCall.campaign_id == campaign_id,
            ProviderCall.status == "dispatched",
        )
        return (await self.session.execute(stmt)).scalars().all()


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
