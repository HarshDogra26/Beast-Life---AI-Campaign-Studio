"""Database rows to API views.
"""

from __future__ import annotations
from collections.abc import Sequence
from typing import Any
from ..domain.enums import RETRYABLE_STAGES, StageName, StageStatus
from ..storage.models import Asset, Campaign, ProviderCall, StageRun
from .schemas import (
    AssetView,
    CampaignSummary,
    CostView,
    ResearchView,
    StageView,
)


def stage_view(row: StageRun) -> StageView:
    stage = StageName(row.stage)
    return StageView(
        stage=stage,
        status=StageStatus(row.status),
        attempt=row.attempt,
        error=row.error,
        error_kind=row.error_kind,
        started_at=row.started_at,
        finished_at=row.finished_at,
        duration_s=row.duration_s,
        retryable=(
            stage in RETRYABLE_STAGES
            and row.status in (StageStatus.FAILED, StageStatus.INTERRUPTED)
        ),
    )


def asset_view(row: Asset) -> AssetView:
    return AssetView(
        id=row.id,
        format=row.format,  # type: ignore[arg-type]
        width=row.width,
        height=row.height,
        byte_size=row.byte_size,
        media_type=row.media_type,
        duration_s=row.duration_s,
        created_at=row.created_at,
        preview_url=f"/api/assets/{row.id}/preview",
        download_url=f"/api/assets/{row.id}/download",
        generation=dict(row.request or {}),
    )


def research_view(report: dict[str, Any] | None) -> ResearchView | None:
    if not report:
        return None
    sources = [
        {
            "id": s["id"],
            "url": str(s["url"]),
            "title": s["title"],
            "accessed_at": s["accessed_at"],
            "excerpt": s.get("excerpt", ""),
            "injection_flags": s.get("injection_flags", []),
        }
        for s in report.get("sources", [])
    ]
    return ResearchView.model_validate(
        {
            "sources": sources,
            "steps": report.get("steps", []),
            "angles": report.get("angles", []),
            "coverage_gap": report.get("coverage_gap"),
            "stop_reason": report.get("stop_reason"),
            "budget": report.get("budget", {}),
            "usage": report.get("usage", {}),
            "is_fixture": report.get("provider_mode") == "fixture",
        }
    )


def campaign_summary(row: Campaign, *, asset_count: int) -> CampaignSummary:
    return CampaignSummary(
        id=row.id,
        title=row.title,
        status=row.status,  # type: ignore[arg-type]
        product_name=(row.brief or {}).get("product_name", ""),
        created_at=row.created_at,
        updated_at=row.updated_at,
        asset_count=asset_count,
        provider_mode=row.provider_mode,
        size_strategy=row.size_strategy,
    )


def cost_view(calls: Sequence[ProviderCall]) -> CostView:
    """Aggregate the ledger, keeping unknowns visible instead of treating them as zero."""
    known = [c.est_cost_usd for c in calls if c.est_cost_usd is not None]
    unknown = sum(1 for c in calls if c.est_cost_usd is None and c.status == "succeeded")
    return CostView(
        provider_calls=len(calls),
        image_calls=sum(c.image_count for c in calls),
        search_credits=sum(c.search_credits for c in calls),
        estimated_cost_usd=round(sum(known), 5) if known else None,
        is_estimate=True,
        unknown_cost_calls=unknown,
        unsettled_calls=sum(1 for c in calls if c.status == "dispatched"),
    )
