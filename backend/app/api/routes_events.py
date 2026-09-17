"""Server-sent events for live stage progress.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from ..domain.enums import CampaignStatus
from ..logging import get_logger
from ..storage.db import session_scope
from ..storage.repo import AssetRepository, CampaignRepository, StageRepository
from .serializers import asset_view, stage_view

log = get_logger(__name__)
router = APIRouter(prefix="/api/campaigns", tags=["events"])

POLL_INTERVAL_S = 1.0
_TERMINAL = {CampaignStatus.COMPLETED, CampaignStatus.FAILED, CampaignStatus.INTERRUPTED}
MAX_STREAM_S = 900


async def _snapshot(campaign_id: str) -> dict[str, Any] | None:
    async with session_scope() as session:
        campaign = await CampaignRepository(session).get(campaign_id)
        if campaign is None:
            return None
        stages = await StageRepository(session).list(campaign_id)
        assets = await AssetRepository(session).list(campaign_id)

    return {
        "campaign_id": campaign_id,
        "status": campaign.status,
        "error": campaign.error,
        "selected_angle_id": campaign.selected_angle_id,
        "updated_at": campaign.updated_at.isoformat(),
        "stages": [s.model_dump(mode="json") for s in (stage_view(r) for r in stages)],
        "assets": [a.model_dump(mode="json") for a in (asset_view(r) for r in assets)],
    }


@router.get("/{campaign_id}/events")
async def campaign_events(campaign_id: str, request: Request) -> StreamingResponse:
    if await _snapshot(campaign_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"campaign {campaign_id} not found")

    async def stream() -> AsyncIterator[bytes]:
        previous: str | None = None
        elapsed = 0.0
        try:
            while elapsed < MAX_STREAM_S:
                if await request.is_disconnected():
                    break

                snapshot = await _snapshot(campaign_id)
                if snapshot is None:
                    yield b'event: gone\ndata: {"reason":"campaign deleted"}\n\n'
                    break

                payload = json.dumps(snapshot, default=str, sort_keys=True)
                if payload != previous:
                    previous = payload
                    yield f"event: snapshot\ndata: {payload}\n\n".encode()
                else:
                    yield b": keepalive\n\n"

                if snapshot["status"] in _TERMINAL:
                    yield b'event: done\ndata: {"reason":"terminal status"}\n\n'
                    break

                await asyncio.sleep(POLL_INTERVAL_S)
                elapsed += POLL_INTERVAL_S
            else:
                yield b'event: done\ndata: {"reason":"stream time limit"}\n\n'
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("sse.stream_failed", campaign_id=campaign_id)
            with contextlib.suppress(Exception):
                yield b'event: error\ndata: {"reason":"stream failed"}\n\n'

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
