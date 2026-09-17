"""Campaign lifecycle endpoints."""

from __future__ import annotations
import secrets
from typing import Annotated, Any
from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, status
from ..domain.brief import ProductBrief
from ..domain.enums import (
    DELIVERABLE_FORMATS,
    CampaignStatus,
    RETRYABLE_STAGES,
    StageName,
    StageStatus,
)
from ..logging import get_logger
from ..security.uploads import UploadRejected, validate_image_upload
from ..storage.artifacts import get_artifact_store, new_artifact_id
from ..storage.db import session_scope
from ..storage.repo import (
    AssetRepository,
    CampaignRepository,
    IdempotencyRepository,
    JobRepository,
    LedgerRepository,
    ResearchRepository,
    SpecRepository,
    StageRepository,
)
from ..worker.runner import JOB_GENERATE, JOB_RESUME, enqueue, retry_stage
from .deps import IdempotencyDep, ProvidersDep, SettingsDep
from .schemas import (
    AcceptedResponse,
    CampaignDetail,
    CampaignSummary,
    CreateCampaignRequest,
    SelectAngleRequest,
)
from .serializers import (
    asset_view,
    campaign_summary,
    cost_view,
    research_view,
    stage_view,
)

log = get_logger(__name__)
router = APIRouter(prefix="/api/campaigns", tags=["campaigns"])


@router.post("", response_model=AcceptedResponse, status_code=status.HTTP_202_ACCEPTED)
async def create_campaign(
    payload: CreateCampaignRequest,
    providers: ProvidersDep,
    idem_key: IdempotencyDep,
) -> AcceptedResponse:
    """Create a campaign and start research.
    """
    scope = "create_campaign"
    if idem_key:
        async with session_scope() as session:
            try:
                previous = await IdempotencyRepository(session).lookup(idem_key, scope)
            except ValueError as exc:
                raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
        if previous:
            log.info("idempotency.replayed", key=idem_key, scope=scope)
            return AcceptedResponse.model_validate(previous)

    campaign_id = f"c_{secrets.token_hex(10)}"
    brief = payload.brief
    title = payload.title or f"{brief.product_name} — {brief.campaign_objective.value}"

    async with session_scope() as session:
        await CampaignRepository(session).create(
            campaign_id=campaign_id,
            title=title,
            brief=brief.model_dump(mode="json"),
            size_strategy=providers.size_strategy.value,
            image_model=providers.image_model,
            provider_mode=providers.mode.value,
        )

    job_id = await enqueue(campaign_id=campaign_id, kind=JOB_GENERATE)
    response = AcceptedResponse(
        campaign_id=campaign_id,
        job_id=job_id,
        status=CampaignStatus.RESEARCHING,
        message="Research started. Poll the campaign for progress.",
    )

    if idem_key:
        async with session_scope() as session:
            await IdempotencyRepository(session).record(
                key=idem_key,
                scope=scope,
                campaign_id=campaign_id,
                response=response.model_dump(mode="json"),
            )
    return response


@router.post("/uploads", status_code=status.HTTP_201_CREATED)
async def upload_reference_image(
    settings: SettingsDep,
    file: Annotated[UploadFile, File(description="Product packshot (PNG/JPEG/WebP).")],
    campaign_hint: Annotated[str, Form()] = "pending",
) -> dict[str, Any]:
    """Accept a reference packshot before the campaign exists.

    The body is read with a hard cap so an oversized upload is rejected without
    being fully buffered, and the type is decided by magic bytes rather than by
    the filename or the declared content type.
    """
    limit = settings.max_upload_bytes
    data = await file.read(limit + 1)
    await file.close()

    try:
        validated = validate_image_upload(
            data,
            declared_type=file.content_type,
            allowed_types=settings.upload_types,
            max_bytes=limit,
        )
    except UploadRejected as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": str(exc), "code": exc.code},
        ) from exc

    artifact_id = new_artifact_id()
    stored = get_artifact_store().write(
        campaign_id="_uploads",
        data=validated.data,
        media_type=validated.media_type,
        artifact_id=artifact_id,
    )
    log.info(
        "upload.accepted",
        artifact_id=artifact_id,
        bytes=stored.byte_size,
        size=f"{validated.width}x{validated.height}",
    )
    return {
        "reference_image_id": artifact_id,
        "width": validated.width,
        "height": validated.height,
        "byte_size": stored.byte_size,
        "media_type": validated.media_type,
    }


@router.get("", response_model=list[CampaignSummary])
async def list_campaigns(
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[CampaignSummary]:
    """Campaign history, newest first. Survives restart — it is read from disk."""
    async with session_scope() as session:
        rows = await CampaignRepository(session).list(limit=limit, offset=offset)
        assets = AssetRepository(session)
        return [
            campaign_summary(
                row,
                asset_count=sum(
                    1
                    for a in await assets.list(row.id)
                    if a.format in {f.value for f in DELIVERABLE_FORMATS}
                ),
            )
            for row in rows
        ]


@router.get("/{campaign_id}", response_model=CampaignDetail)
async def get_campaign(campaign_id: str) -> CampaignDetail:
    """Full campaign state. This is what makes a saved campaign reopenable."""
    async with session_scope() as session:
        campaign = await CampaignRepository(session).get(campaign_id)
        if campaign is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"campaign {campaign_id} not found")

        stages = await StageRepository(session).list(campaign_id)
        report = await ResearchRepository(session).get(campaign_id)
        spec_row = await SpecRepository(session).latest_for_campaign(campaign_id)
        assets = await AssetRepository(session).list(campaign_id)
        ledger = await LedgerRepository(session).list(campaign_id)
        active = await JobRepository(session).active_for_campaign(campaign_id)

    return CampaignDetail(
        id=campaign.id,
        title=campaign.title,
        status=campaign.status,  # type: ignore[arg-type]
        brief=dict(campaign.brief or {}),
        selected_angle_id=campaign.selected_angle_id,
        provider_mode=campaign.provider_mode,
        size_strategy=campaign.size_strategy,
        image_model=campaign.image_model,
        error=campaign.error,
        created_at=campaign.created_at,
        updated_at=campaign.updated_at,
        stages=[stage_view(s) for s in stages],
        research=research_view(report),
        spec=dict(spec_row.spec) if spec_row else None,
        assets=[asset_view(a) for a in assets],
        cost=cost_view(ledger),
        active_job=active.id if active else None,
    )


@router.post("/{campaign_id}/select", response_model=AcceptedResponse)
async def select_angle(
    campaign_id: str,
    payload: SelectAngleRequest,
    idem_key: IdempotencyDep,
) -> AcceptedResponse:
    """Choose one angle and begin asset generation."""
    scope = f"select:{campaign_id}"
    if idem_key:
        async with session_scope() as session:
            previous = await IdempotencyRepository(session).lookup(idem_key, scope)
        if previous:
            return AcceptedResponse.model_validate(previous)

    async with session_scope() as session:
        campaign = await CampaignRepository(session).get(campaign_id)
        if campaign is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"campaign {campaign_id} not found")

        report = await ResearchRepository(session).get(campaign_id)
        valid = {a["id"] for a in (report or {}).get("angles", [])}
        if payload.angle_id not in valid:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": f"{payload.angle_id} is not one of the proposed angles",
                    "code": "unknown_angle",
                    "detail": sorted(valid),
                },
            )

        if active := await JobRepository(session).active_for_campaign(campaign_id):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "error": "Generation is already running for this campaign.",
                    "code": "job_in_flight",
                    "detail": {"job_id": active.id},
                },
            )
        if campaign.status not in (
            CampaignStatus.AWAITING_SELECTION,
            CampaignStatus.INTERRUPTED,
        ):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "error": f"Campaign is {campaign.status}, not awaiting selection.",
                    "code": "wrong_status",
                },
            )

    job_id = await enqueue(
        campaign_id=campaign_id, kind=JOB_RESUME, payload={"angle_id": payload.angle_id}
    )
    response = AcceptedResponse(
        campaign_id=campaign_id,
        job_id=job_id,
        status=CampaignStatus.GENERATING,
        message=f"Generating assets from angle {payload.angle_id}.",
    )
    if idem_key:
        async with session_scope() as session:
            await IdempotencyRepository(session).record(
                key=idem_key,
                scope=scope,
                campaign_id=campaign_id,
                response=response.model_dump(mode="json"),
            )
    return response


@router.post("/{campaign_id}/stages/{stage}/retry", response_model=AcceptedResponse)
async def retry_campaign_stage(
    campaign_id: str, stage: StageName, idem_key: IdempotencyDep
) -> AcceptedResponse:
    """Retry one failed stage.
    """
    if stage not in RETRYABLE_STAGES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"error": f"{stage} is not retryable", "code": "stage_not_retryable"},
        )

    scope = f"retry:{campaign_id}:{stage.value}"
    if idem_key:
        async with session_scope() as session:
            previous = await IdempotencyRepository(session).lookup(idem_key, scope)
        if previous:
            return AcceptedResponse.model_validate(previous)

    async with session_scope() as session:
        if await CampaignRepository(session).get(campaign_id) is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"campaign {campaign_id} not found")
        if active := await JobRepository(session).active_for_campaign(campaign_id):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "error": "A job is already running for this campaign.",
                    "code": "job_in_flight",
                    "detail": {"job_id": active.id},
                },
            )
        row = await StageRepository(session).get(campaign_id, stage)
        if row is None or row.status not in (StageStatus.FAILED, StageStatus.INTERRUPTED):
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                detail={
                    "error": (
                        f"{stage} is {row.status if row else 'not started'}; "
                        "only failed or interrupted stages can be retried."
                    ),
                    "code": "stage_not_failed",
                },
            )

    job_id = await retry_stage(campaign_id=campaign_id, stage=stage)
    response = AcceptedResponse(
        campaign_id=campaign_id,
        job_id=job_id,
        status=CampaignStatus.GENERATING,
        message=f"Retrying {stage.value}; completed stages will be reused.",
    )
    if idem_key:
        async with session_scope() as session:
            await IdempotencyRepository(session).record(
                key=idem_key,
                scope=scope,
                campaign_id=campaign_id,
                response=response.model_dump(mode="json"),
            )
    return response


@router.get("/{campaign_id}/ledger")
async def get_ledger(campaign_id: str) -> dict[str, Any]:
    """Per-call provider usage. Unsettled rows are possible orphaned spend."""
    async with session_scope() as session:
        repo = LedgerRepository(session)
        calls = await repo.list(campaign_id)
        unsettled = await repo.unsettled(campaign_id)

    return {
        "summary": cost_view(calls).model_dump(),
        "calls": [
            {
                "id": c.id,
                "stage": c.stage,
                "provider": c.provider,
                "model": c.model,
                "operation": c.operation,
                "status": c.status,
                "latency_ms": c.latency_ms,
                "prompt_tokens": c.prompt_tokens,
                "completion_tokens": c.completion_tokens,
                "image_tokens": c.image_tokens,
                "image_count": c.image_count,
                "est_cost_usd": c.est_cost_usd,
                "is_estimate": c.is_estimate,
                "error": c.error,
                "created_at": c.created_at,
            }
            for c in calls
        ],
        "unsettled_warning": (
            None
            if not unsettled
            else (
                f"{len(unsettled)} provider call(s) were dispatched but never "
                "settled. They may have been billed without a result reaching us — "
                "this is the residual duplicate-work risk a client-side idempotency "
                "key cannot eliminate."
            )
        ),
    }
