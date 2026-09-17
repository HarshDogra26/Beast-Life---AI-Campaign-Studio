"""The human pause: the user inspects the research and picks one angle.
"""

from __future__ import annotations
from typing import Any
from langgraph.types import interrupt
from ...domain.enums import StageName, StageStatus
from ...logging import get_logger
from ...storage.db import session_scope
from ...storage.repo import CampaignRepository, StageRepository
from ..context import RunContext
from ..state import CampaignState

log = get_logger(__name__)


class InvalidSelectionError(ValueError):
    """The resumed value did not identify one of the proposed angles."""


async def await_selection_node(state: CampaignState, ctx: RunContext) -> dict[str, Any]:
    angles = state.get("angles") or []
    valid_ids = {a["id"] for a in angles}

    existing = state.get("selected_angle_id")
    if existing in valid_ids:
        log.info("selection.already_made", angle_id=existing)
        await _mark(ctx, StageStatus.REUSED)
        return {"selected_angle_id": existing, "notes": [f"angle {existing} already selected"]}

    await _mark(ctx, StageStatus.AWAITING_INPUT)
    log.info("selection.awaiting", options=sorted(valid_ids))

    chosen = interrupt(
        {
            "reason": "angle_selection_required",
            "campaign_id": ctx.campaign_id,
            "angles": [
                {"id": a["id"], "title": a["title"], "hook": a["hook"]} for a in angles
            ],
        }
    )

    angle_id = chosen.get("angle_id") if isinstance(chosen, dict) else chosen
    if angle_id not in valid_ids:
        raise InvalidSelectionError(
            f"{angle_id!r} is not one of the proposed angles {sorted(valid_ids)}"
        )

    async with session_scope() as session:
        await CampaignRepository(session).set_selected_angle(ctx.campaign_id, angle_id)
        await StageRepository(session).complete_stage(
            campaign_id=ctx.campaign_id,
            stage=StageName.AWAIT_SELECTION,
            output={"selected_angle_id": angle_id},
        )

    log.info("selection.received", angle_id=angle_id)
    return {"selected_angle_id": angle_id, "notes": [f"user selected angle {angle_id}"]}


async def _mark(ctx: RunContext, status: StageStatus) -> None:
    async with session_scope() as session:
        repo = StageRepository(session)
        await repo.begin_stage(
            campaign_id=ctx.campaign_id,
            stage=StageName.AWAIT_SELECTION,
            input_fingerprint="human",
            worker_epoch=ctx.worker_epoch,
        )
        await repo.set_status(
            campaign_id=ctx.campaign_id, stage=StageName.AWAIT_SELECTION, status=status
        )
