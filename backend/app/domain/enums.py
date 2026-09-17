"""Enumerations shared across every contract.
"""

from __future__ import annotations
from enum import StrEnum


class StageName(StrEnum):
    """The explicit workflow stages. Order here is the canonical display order."""

    VALIDATE_BRIEF = "validate_brief"
    RESEARCH = "research"
    SYNTHESIZE_ANGLES = "synthesize_angles"
    AWAIT_SELECTION = "await_selection"
    BUILD_SPEC = "build_spec"
    MASTER_SCENE = "master_scene"
    RENDER_SQUARE = "render_square"
    RENDER_VERTICAL = "render_vertical"
    RENDER_VIDEO = "render_video"


RETRYABLE_STAGES: frozenset[StageName] = frozenset(
    {
        StageName.RESEARCH,
        StageName.SYNTHESIZE_ANGLES,
        StageName.BUILD_SPEC,
        StageName.MASTER_SCENE,
        StageName.RENDER_SQUARE,
        StageName.RENDER_VERTICAL,
        StageName.RENDER_VIDEO,
    }
)

STAGE_DEPENDENCIES: dict[StageName, tuple[StageName, ...]] = {
    StageName.VALIDATE_BRIEF: (),
    StageName.RESEARCH: (StageName.VALIDATE_BRIEF,),
    StageName.SYNTHESIZE_ANGLES: (StageName.RESEARCH,),
    StageName.AWAIT_SELECTION: (StageName.SYNTHESIZE_ANGLES,),
    StageName.BUILD_SPEC: (StageName.AWAIT_SELECTION,),
    StageName.MASTER_SCENE: (StageName.BUILD_SPEC,),
    StageName.RENDER_SQUARE: (StageName.MASTER_SCENE,),
    StageName.RENDER_VERTICAL: (StageName.MASTER_SCENE,),
    StageName.RENDER_VIDEO: (StageName.RENDER_VERTICAL,),
}


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    REUSED = "reused"
    AWAITING_INPUT = "awaiting_input"


TERMINAL_STAGE_STATUSES: frozenset[StageStatus] = frozenset(
    {StageStatus.COMPLETED, StageStatus.REUSED}
)


class CampaignStatus(StrEnum):
    DRAFT = "draft"
    RESEARCHING = "researching"
    AWAITING_SELECTION = "awaiting_selection"
    GENERATING = "generating"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


class AssetFormat(StrEnum):
    """Export targets named by the assignment."""

    SQUARE = "square_1080x1080"
    VERTICAL = "vertical_1080x1920"
    VIDEO = "video_1080x1920"
    MASTER_SCENE = "master_scene"
    VERTICAL_PLATE = "vertical_plate"


DELIVERABLE_FORMATS: frozenset[AssetFormat] = frozenset(
    {AssetFormat.SQUARE, AssetFormat.VERTICAL, AssetFormat.VIDEO}
)


class CampaignObjective(StrEnum):
    INTRODUCE = "introduce_product"
    AWARENESS = "build_awareness"
    CONSIDERATION = "drive_consideration"
    TRAFFIC = "drive_traffic"
    RETENTION = "retain_customers"


class ProviderMode(StrEnum):
    LIVE = "live"
    FIXTURE = "fixture"


class SizeStrategy(StrEnum):
    """How we bridge model output sizes to the 1080-px export targets.
    """

    NATIVE = "native"
    BANDS = "bands"


class ToolName(StrEnum):
    """The complete action surface available to the research agent.
    """

    WEB_SEARCH = "web_search"
    FETCH_PAGE = "fetch_page"
