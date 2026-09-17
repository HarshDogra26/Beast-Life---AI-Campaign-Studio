"""Brief validation as an explicit workflow stage.
"""

from __future__ import annotations
from typing import Any
from ...domain.brief import ProductBrief
from ...domain.enums import StageName
from ...logging import get_logger
from ..context import RunContext, run_stage
from ..state import CampaignState

log = get_logger(__name__)


async def validate_brief_node(state: CampaignState, ctx: RunContext) -> dict[str, Any]:
    raw = state["brief"]

    async def work() -> dict[str, Any]:
        brief = ProductBrief.model_validate(raw)
        log.info(
            "brief.validated",
            product=brief.product_name,
            verified_claims=len(brief.verified_claims),
            has_reference=bool(brief.reference_image_id),
        )
        return {
            "brief": brief.model_dump(mode="json"),
            "claim_corpus_chars": len(brief.claim_corpus()),
        }

    output = await run_stage(
        ctx, StageName.VALIDATE_BRIEF, inputs={"brief": raw}, work=work
    )
    return {
        "brief": (output or {}).get("brief", raw),
        "notes": ["brief validated"],
    }
