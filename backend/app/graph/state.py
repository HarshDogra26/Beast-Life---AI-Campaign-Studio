"""Graph state.
"""

from __future__ import annotations
import operator
from typing import Annotated, Any, TypedDict


def merge_assets(
    left: dict[str, dict[str, Any]], right: dict[str, dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Reducer for concurrent writes to ``assets``.
    """
    return {**left, **right}


def merge_errors(left: dict[str, str], right: dict[str, str]) -> dict[str, str]:
    return {**left, **right}


class CampaignState(TypedDict, total=False):
    """Everything the workflow carries between stages."""

    campaign_id: str
    run_id: str
    brief: dict[str, Any]
    size_strategy: str
    research: dict[str, Any] | None
    angles: list[dict[str, Any]]
    selected_angle_id: str | None
    spec: dict[str, Any] | None

    assets: Annotated[dict[str, dict[str, Any]], merge_assets]

    errors: Annotated[dict[str, str], merge_errors]

    notes: Annotated[list[str], operator.add]


def initial_state(
    *,
    campaign_id: str,
    run_id: str,
    brief: dict[str, Any],
    size_strategy: str,
    selected_angle_id: str | None = None,
) -> CampaignState:
    return CampaignState(
        campaign_id=campaign_id,
        run_id=run_id,
        brief=brief,
        size_strategy=size_strategy,
        research=None,
        angles=[],
        selected_angle_id=selected_angle_id,
        spec=None,
        assets={},
        errors={},
        notes=[],
    )
