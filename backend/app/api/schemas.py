"""HTTP request and response models.
"""

from __future__ import annotations
from datetime import datetime
from typing import Any
from pydantic import BaseModel, ConfigDict, Field
from ..domain.brief import ProductBrief
from ..domain.enums import AssetFormat, CampaignStatus, StageName, StageStatus


class CreateCampaignRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    brief: ProductBrief
    title: str | None = Field(default=None, max_length=200)


class SelectAngleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    angle_id: str = Field(pattern=r"^a[1-9]\d?$")


class StageView(BaseModel):
    stage: StageName
    status: StageStatus
    attempt: int
    error: str | None = None
    error_kind: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_s: float | None = None
    retryable: bool = False


class SourceView(BaseModel):
    """A source card for the research review screen."""

    id: str
    url: str
    title: str
    accessed_at: datetime
    excerpt: str
    injection_flags: list[str] = Field(default_factory=list)


class ToolCallView(BaseModel):
    tool: str
    arguments: dict[str, Any]
    latency_ms: int
    ok: bool
    result_summary: str
    error: str | None = None
    credits_spent: int = 0


class AgentStepView(BaseModel):
    index: int
    decision_summary: str
    tool_calls: list[ToolCallView] = Field(default_factory=list)


class AngleView(BaseModel):
    id: str
    title: str
    audience_insight: str
    insight_source_ids: list[str]
    hook: str
    visual_direction: str
    rationale: str
    supporting_source_ids: list[str]


class ResearchView(BaseModel):
    sources: list[SourceView] = Field(default_factory=list)
    steps: list[AgentStepView] = Field(default_factory=list)
    angles: list[AngleView] = Field(default_factory=list)
    coverage_gap: str | None = None
    stop_reason: str | None = None
    budget: dict[str, Any] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    is_fixture: bool = False


class AssetView(BaseModel):
    id: str
    format: AssetFormat
    width: int
    height: int
    byte_size: int
    media_type: str
    duration_s: float | None = None
    created_at: datetime
    preview_url: str
    download_url: str
    generation: dict[str, Any] = Field(default_factory=dict)


class CostView(BaseModel):
    """Usage ledger. Every USD figure is an estimate and says so."""

    provider_calls: int = 0
    image_calls: int = 0
    search_credits: int = 0
    estimated_cost_usd: float | None = None
    is_estimate: bool = True
    unknown_cost_calls: int = 0
    unsettled_calls: int = 0
    note: str = (
        "Image models bill per token, not per image, so this is an estimate. "
        "Tavily bills in credits against a plan and is not converted to USD."
    )


class CampaignSummary(BaseModel):
    id: str
    title: str
    status: CampaignStatus
    product_name: str
    created_at: datetime
    updated_at: datetime
    asset_count: int = 0
    provider_mode: str
    size_strategy: str


class CampaignDetail(BaseModel):
    id: str
    title: str
    status: CampaignStatus
    brief: dict[str, Any]
    selected_angle_id: str | None = None
    provider_mode: str
    size_strategy: str
    image_model: str
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    stages: list[StageView] = Field(default_factory=list)
    research: ResearchView | None = None
    spec: dict[str, Any] | None = None
    assets: list[AssetView] = Field(default_factory=list)
    cost: CostView = Field(default_factory=CostView)
    active_job: str | None = None
    notes: list[str] = Field(default_factory=list)


class AcceptedResponse(BaseModel):
    campaign_id: str
    job_id: str
    status: CampaignStatus
    message: str


class ErrorResponse(BaseModel):
    error: str
    code: str
    detail: Any | None = None


class HealthResponse(BaseModel):
    status: str
    provider_mode: str
    image_model: str
    image_model_family: str
    size_strategy: str
    ffmpeg: str
    database: str
    mcp_url: str
    worker_epoch: str
    notes: list[str] = Field(default_factory=list)
    failure_injection: list[str] = Field(default_factory=list)
