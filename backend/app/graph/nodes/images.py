"""Image stages: one master scene, then two coordinated re-frames.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any
from ... import prompts
from ...domain.assets import (
    EXPORT_SIZES,
    MASTER_SCENE_SIZE,
    GenerationRequest,
    RenderedAsset,
    generation_size,
)
from ...domain.enums import AssetFormat, SizeStrategy, StageName
from ...domain.spec import CampaignSpec
from ...logging import get_logger
from ...providers.base import ImageResult, SizeRejectedError
from ...providers.pricing import estimate_image_cost_usd
from ...providers.retry import with_retry
from ...rendering.layout import build_plate, compose_ad, encode_png
from ..context import RunContext, ledger_dispatch, ledger_settle, run_stage, save_asset
from ..state import CampaignState

log = get_logger(__name__)

PNG = "image/png"

_FORMAT_STAGE = {
    AssetFormat.SQUARE: StageName.RENDER_SQUARE,
    AssetFormat.VERTICAL: StageName.RENDER_VERTICAL,
}

_FORMAT_LABEL = {
    AssetFormat.SQUARE: "1:1 square social feed",
    AssetFormat.VERTICAL: "9:16 vertical full-screen story",
}

_COMPOSITION_KEY = {AssetFormat.SQUARE: "square", AssetFormat.VERTICAL: "vertical"}


# --------------------------------------------------------------------------
# master scene
# --------------------------------------------------------------------------


async def master_scene_node(state: CampaignState, ctx: RunContext) -> dict[str, Any]:
    spec = CampaignSpec.model_validate(state["spec"])

    async def work() -> dict[str, Any]:
        return await _generate_master(ctx, spec)

    output = await run_stage(
        ctx,
        StageName.MASTER_SCENE,
        inputs={"spec": spec.fingerprint(), "model": ctx.providers.image_model},
        work=work,
    )
    return {
        "assets": {AssetFormat.MASTER_SCENE.value: output},
        "notes": ["master scene generated (identity anchor for both formats)"],
    }


async def _generate_master(ctx: RunContext, spec: CampaignSpec) -> dict[str, Any]:
    prompt = prompts.render(
        "image_master_scene",
        scene_setting=spec.scene.setting,
        scene_subject=spec.scene.subject,
        scene_lighting=spec.scene.lighting,
        scene_mood=spec.scene.mood,
        scene_props=", ".join(spec.scene.props) or "none",
        product_form_factor=spec.product.form_factor,
        product_packaging=spec.product.packaging_description,
        palette_primary=spec.palette.primary,
        palette_secondary=spec.palette.secondary,
        palette_accent=spec.palette.accent,
    )

    reference = _load_reference(ctx, spec)
    size = str(MASTER_SCENE_SIZE)
    quality = ctx.providers.capabilities.coerce_quality(ctx.settings.image_quality)

    async def call() -> ImageResult:
        if reference is not None:
            return await ctx.providers.image.edit(
                prompt=(
                    "Place the product from the supplied photograph into this scene, "
                    "preserving its exact shape, proportions, colour and finish.\n\n"
                    + prompt
                ),
                size=size,
                quality=quality,
                images=[("packshot.png", reference, PNG)],
                input_fidelity=ctx.settings.image_input_fidelity,
            )
        return await ctx.providers.image.generate(prompt=prompt, size=size, quality=quality)

    result = await _invoke(
        ctx,
        stage=StageName.MASTER_SCENE,
        operation="edits" if reference is not None else "generations",
        call=call,
    )

    stored = ctx.artifacts.write(
        campaign_id=ctx.campaign_id, data=result.data, media_type=result.media_type
    )
    request = GenerationRequest(
        format=AssetFormat.MASTER_SCENE,
        operation="edits" if reference is not None else "generations",
        model=result.model or ctx.providers.image_model,
        prompt=prompt,
        generated_size=MASTER_SCENE_SIZE,
        export_size=MASTER_SCENE_SIZE,
        quality=quality,
        input_fidelity=ctx.settings.image_input_fidelity if reference else None,
        strategy=SizeStrategy(ctx.providers.size_strategy),
        source_asset_id=spec.product.reference_image_id,
    )
    asset = RenderedAsset(
        id=stored.artifact_id,
        campaign_id=ctx.campaign_id,
        spec_id=spec.spec_id,
        format=AssetFormat.MASTER_SCENE,
        artifact_path=stored.relative_path,
        media_type=stored.media_type,
        width=MASTER_SCENE_SIZE.width,
        height=MASTER_SCENE_SIZE.height,
        byte_size=stored.byte_size,
        sha256=stored.sha256,
        request=request,
    )
    payload = asset.model_dump(mode="json")
    await save_asset(payload)
    log.info("master_scene.saved", asset_id=asset.id, bytes=stored.byte_size)
    return payload


# --------------------------------------------------------------------------
# per-format renders (run concurrently)
# --------------------------------------------------------------------------


async def render_square_node(state: CampaignState, ctx: RunContext) -> dict[str, Any]:
    return await _render_format(state, ctx, AssetFormat.SQUARE)


async def render_vertical_node(state: CampaignState, ctx: RunContext) -> dict[str, Any]:
    return await _render_format(state, ctx, AssetFormat.VERTICAL)


async def _render_format(
    state: CampaignState, ctx: RunContext, fmt: AssetFormat
) -> dict[str, Any]:
    spec = CampaignSpec.model_validate(state["spec"])
    master = (state.get("assets") or {}).get(AssetFormat.MASTER_SCENE.value)
    if not master:
        raise ValueError("master scene is missing; cannot re-frame a scene that does not exist")

    async def work() -> dict[str, Any]:
        return await _reframe(ctx, spec, fmt, master)

    output = await run_stage(
        ctx,
        _FORMAT_STAGE[fmt],
        inputs={
            "spec": spec.fingerprint(),
            "master": master["sha256"],
            "format": fmt.value,
            "strategy": ctx.providers.size_strategy.value,
        },
        work=work,
    )
    return {
        "assets": {fmt.value: output},
        "notes": [f"{fmt.value} rendered at {EXPORT_SIZES[fmt]}"],
    }


async def _reframe(
    ctx: RunContext, spec: CampaignSpec, fmt: AssetFormat, master: dict[str, Any]
) -> dict[str, Any]:
    strategy = SizeStrategy(ctx.providers.size_strategy)
    guidance = spec.composition[_COMPOSITION_KEY[fmt]]
    master_bytes = ctx.artifacts.read(master["artifact_path"])

    prompt = prompts.render(
        "image_reframe",
        format_label=_FORMAT_LABEL[fmt],
        framing=guidance.framing,
        product_placement=guidance.product_placement,
        text_safe_zone=guidance.text_safe_zone,
    )
    quality = ctx.providers.capabilities.coerce_quality(ctx.settings.image_quality)

    async def attempt(active: SizeStrategy) -> tuple[ImageResult, SizeStrategy]:
        size = generation_size(fmt, active)

        async def call() -> ImageResult:
            return await ctx.providers.image.edit(
                prompt=prompt,
                size=str(size),
                quality=quality,
                images=[("master_scene.png", master_bytes, PNG)],
                input_fidelity=ctx.settings.image_input_fidelity,
            )

        return await _invoke(ctx, stage=_FORMAT_STAGE[fmt], operation="edits", call=call), active

    try:
        result, strategy = await attempt(strategy)
    except SizeRejectedError as exc:
        if strategy is SizeStrategy.BANDS:
            raise
        log.warning("render.size_rejected_degrading", format=fmt.value, error=str(exc)[:200])
        ctx.providers.size_strategy = SizeStrategy.BANDS
        result, strategy = await attempt(SizeStrategy.BANDS)

    generated = generation_size(fmt, strategy)
    target = EXPORT_SIZES[fmt]

    # Deterministic composition: exact size, exact copy, measured contrast.
    composed = compose_ad(result.data, spec, fmt, strategy=strategy)

    if fmt is AssetFormat.VERTICAL:
        await _save_plate(ctx, spec, result.data, strategy, master_id=master["id"])

    stored = ctx.artifacts.write(
        campaign_id=ctx.campaign_id, data=composed, media_type=PNG
    )
    request = GenerationRequest(
        format=fmt,
        operation="edits",
        model=result.model or ctx.providers.image_model,
        prompt=prompt,
        generated_size=generated,
        export_size=target,
        quality=quality,
        input_fidelity=ctx.settings.image_input_fidelity,
        strategy=strategy,
        source_asset_id=master["id"],
    )
    asset = RenderedAsset(
        id=stored.artifact_id,
        campaign_id=ctx.campaign_id,
        spec_id=spec.spec_id,
        format=fmt,
        artifact_path=stored.relative_path,
        media_type=PNG,
        width=target.width,
        height=target.height,
        byte_size=stored.byte_size,
        sha256=stored.sha256,
        request=request,
    )
    payload = asset.model_dump(mode="json")
    await save_asset(payload)
    log.info(
        "render.saved",
        format=fmt.value,
        generated=str(generated),
        exported=str(target),
        strategy=strategy.value,
    )
    return payload


async def _save_plate(
    ctx: RunContext,
    spec: CampaignSpec,
    raw_image: bytes,
    strategy: SizeStrategy,
    *,
    master_id: str,
) -> None:
    """Persist the copy-free 1080x1920 plate for the video stage.
    """
    fmt = AssetFormat.VERTICAL_PLATE
    plate = build_plate(raw_image, spec, AssetFormat.VERTICAL, strategy=strategy)
    stored = ctx.artifacts.write(
        campaign_id=ctx.campaign_id, data=encode_png(plate), media_type=PNG
    )
    target = EXPORT_SIZES[fmt]
    asset = RenderedAsset(
        id=stored.artifact_id,
        campaign_id=ctx.campaign_id,
        spec_id=spec.spec_id,
        format=fmt,
        artifact_path=stored.relative_path,
        media_type=PNG,
        width=target.width,
        height=target.height,
        byte_size=stored.byte_size,
        sha256=stored.sha256,
        request=GenerationRequest(
            format=fmt,
            operation="composite",
            model="pillow",
            prompt="Copy-free plate of the vertical ad, used as the video's base frame.",
            generated_size=generation_size(AssetFormat.VERTICAL, strategy),
            export_size=target,
            strategy=strategy,
            source_asset_id=master_id,
        ),
    )
    await save_asset(asset.model_dump(mode="json"))


# --------------------------------------------------------------------------
# shared provider invocation
# --------------------------------------------------------------------------


async def _invoke(
    ctx: RunContext, *, stage: StageName, operation: str, call
) -> ImageResult:
    """Ledger, rate limit, and retry one image provider call."""
    call_id = f"img_{uuid.uuid4().hex[:16]}"
    await ledger_dispatch(
        ctx,
        call_id=call_id,
        stage=stage.value,
        provider=ctx.providers.image.name,
        model=ctx.providers.image_model,
        operation=operation,
    )
    started = time.monotonic()

    async def guarded() -> ImageResult:
        async with ctx.providers.image_gate:
            return await call()

    try:
        result = await with_retry(
            guarded,
            attempts=ctx.settings.max_retries,
            base_delay_s=ctx.settings.retry_base_delay_s,
            label=f"{stage.value}:{operation}",
        )
    except asyncio.CancelledError:
        # CancelledError is a BaseException, so `except Exception` misses it and
        # the ledger row would stay "dispatched" forever — reported as possibly
        # billed with no result. We know better: it was cancelled. Record that
        # honestly, noting the request may still have been in flight.
        await ledger_settle(
            call_id=call_id,
            status="cancelled",
            latency_ms=int((time.monotonic() - started) * 1000),
            error=(
                "Cancelled because a concurrent stage failed. The request may have "
                "reached the provider before cancellation took effect."
            ),
        )
        raise
    except Exception as exc:
        await ledger_settle(
            call_id=call_id,
            status="failed",
            latency_ms=int((time.monotonic() - started) * 1000),
            error=str(exc)[:500],
        )
        raise

    await ledger_settle(
        call_id=call_id,
        status="succeeded",
        latency_ms=int((time.monotonic() - started) * 1000),
        image_tokens=result.output_tokens,
        image_count=1,
        est_cost_usd=estimate_image_cost_usd(
            model=result.model or ctx.providers.image_model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        ),
        is_estimate=True,
    )
    return result


def _load_reference(ctx: RunContext, spec: CampaignSpec) -> bytes | None:
    """Load the uploaded packshot, if one was supplied and still exists.
    """
    ref_id = spec.product.reference_image_id
    if not ref_id:
        return None

    for relative in (f"{ctx.campaign_id}/{ref_id}.png", f"_uploads/{ref_id}.png"):
        if ctx.artifacts.exists(relative):
            log.info("master_scene.reference_loaded", reference_id=ref_id, path=relative)
            return ctx.artifacts.read(relative)

    log.warning("master_scene.reference_missing", reference_id=ref_id)
    return None
