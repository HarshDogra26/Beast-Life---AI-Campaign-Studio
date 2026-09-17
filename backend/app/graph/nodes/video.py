"""Video stage: render the vertical cut from the approved assets.
"""

from __future__ import annotations
import shutil
from pathlib import Path
from typing import Any
from ...domain.assets import EXPORT_SIZES, GenerationRequest, RenderedAsset
from ...domain.enums import AssetFormat, SizeStrategy, StageName
from ...domain.spec import CampaignSpec
from ...logging import get_logger
from ...rendering.video import (
    FFmpegMissingError,
    VideoRenderError,
    build_keyframes,
    render_video,
)
from ...storage.db import session_scope
from ...storage.repo import AssetRepository
from ..context import RunContext, run_stage, save_asset
from ..state import CampaignState

log = get_logger(__name__)


async def render_video_node(state: CampaignState, ctx: RunContext) -> dict[str, Any]:
    spec = CampaignSpec.model_validate(state["spec"])
    assets = state.get("assets") or {}
    vertical = assets.get(AssetFormat.VERTICAL.value)
    if not vertical:
        raise ValueError("vertical asset is missing; the video is derived from it")

    plate = assets.get(AssetFormat.VERTICAL_PLATE.value) or await _lookup_plate(ctx)
    base = plate or vertical

    async def work() -> dict[str, Any]:
        return await _render(ctx, spec, vertical, base)

    output = await run_stage(
        ctx,
        StageName.RENDER_VIDEO,
        inputs={
            "spec": spec.fingerprint(),
            "vertical": vertical["sha256"],
            "target_s": spec.video.total_seconds,
        },
        work=work,
    )
    return {
        "assets": {AssetFormat.VIDEO.value: output},
        "notes": [f"video rendered ({(output or {}).get('duration_s', '?')}s)"],
    }


async def _lookup_plate(ctx: RunContext) -> dict[str, Any] | None:
    """Find a previously saved plate, e.g. when the video is retried on its own."""
    async with session_scope() as session:
        row = await AssetRepository(session).by_format(
            ctx.campaign_id, AssetFormat.VERTICAL_PLATE.value
        )
    if row is None:
        return None
    return {"id": row.id, "artifact_path": row.artifact_path, "sha256": row.sha256}


async def _render(
    ctx: RunContext, spec: CampaignSpec, vertical: dict[str, Any], base: dict[str, Any]
) -> dict[str, Any]:
    source = ctx.artifacts.read(base["artifact_path"])
    frames = build_keyframes(source, spec)

    work_dir = Path(ctx.artifacts.reserve_path(ctx.campaign_id, f"video_work_{spec.spec_id}"))
    output_path = Path(ctx.artifacts.reserve_path(ctx.campaign_id, f"{spec.spec_id}_ad.mp4"))

    try:
        probe = await render_video(
            frames=frames,
            spec=spec,
            work_dir=work_dir,
            output_path=output_path,
            ffmpeg_bin=ctx.settings.ffmpeg_bin,
            ffprobe_bin=ctx.settings.ffprobe_bin,
            timeout_s=ctx.settings.video_timeout_s,
        )
    except (FFmpegMissingError, VideoRenderError):
        output_path.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    data = output_path.read_bytes()
    stored = ctx.artifacts.write(
        campaign_id=ctx.campaign_id, data=data, media_type="video/mp4"
    )
    output_path.unlink(missing_ok=True)

    target = EXPORT_SIZES[AssetFormat.VIDEO]
    request = GenerationRequest(
        format=AssetFormat.VIDEO,
        operation="render",
        model="ffmpeg+pillow",
        prompt=_storyboard(spec),
        generated_size=target,
        export_size=target,
        strategy=SizeStrategy(ctx.providers.size_strategy),
        source_asset_id=vertical["id"],
    )
    asset = RenderedAsset(
        id=stored.artifact_id,
        campaign_id=ctx.campaign_id,
        spec_id=spec.spec_id,
        format=AssetFormat.VIDEO,
        artifact_path=stored.relative_path,
        media_type="video/mp4",
        width=probe.width,
        height=probe.height,
        byte_size=stored.byte_size,
        sha256=stored.sha256,
        duration_s=round(probe.duration_s, 3),
        request=request,
    )
    payload = asset.model_dump(mode="json")
    await save_asset(payload)
    log.info("video.saved", asset_id=asset.id, duration_s=asset.duration_s)
    return payload


def _storyboard(spec: CampaignSpec) -> str:
    """Human-readable record of what was rendered, stored with the asset."""
    lines = [
        f"Storyboard for spec {spec.spec_id} "
        f"({spec.video.total_seconds}s, {spec.video.transition} transitions)"
    ]
    for index, beat in enumerate(spec.video.beats, start=1):
        lines.append(
            f"{index}. [{beat.label}] {beat.seconds}s — motion: {beat.motion} — "
            f'text: "{beat.on_screen_text}"'
        )
    return "\n".join(lines)
