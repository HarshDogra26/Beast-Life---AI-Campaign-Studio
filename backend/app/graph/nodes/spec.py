"""Build the shared creative specification from the selected angle.
"""

from __future__ import annotations
import secrets
import time
import uuid
from typing import Any
from pydantic import ValidationError
from ... import prompts
from ...domain.enums import StageName
from ...domain.spec import CampaignSpec
from ...logging import get_logger
from ...providers.azure_llm import parse_json_response
from ...providers.base import MalformedOutputError
from ...providers.pricing import estimate_chat_cost_usd
from ...storage.db import session_scope
from ...storage.repo import SpecRepository
from ..context import RunContext, ledger_dispatch, ledger_settle, retrying, run_stage
from ..schemas import CAMPAIGN_SPEC_SCHEMA
from ..state import CampaignState

log = get_logger(__name__)

MAX_REPAIRS = 2


async def build_spec_node(state: CampaignState, ctx: RunContext) -> dict[str, Any]:
    brief = state["brief"]
    angle_id = state["selected_angle_id"]
    angle = next((a for a in state.get("angles", []) if a["id"] == angle_id), None)
    if angle is None:
        raise ValueError(f"selected angle {angle_id!r} is not among the proposed angles")

    async def work() -> dict[str, Any]:
        spec = await _build(ctx, brief, angle)
        async with session_scope() as session:
            await SpecRepository(session).save(
                spec=spec, fingerprint_value=CampaignSpec.model_validate(spec).fingerprint()
            )
        return spec

    output = await run_stage(
        ctx,
        StageName.BUILD_SPEC,
        inputs={"brief": brief, "angle": angle},
        work=work,
    )
    return {
        "spec": output,
        "notes": [f"spec {(output or {}).get('spec_id', '?')} built from angle {angle_id}"],
    }


async def _build(
    ctx: RunContext, brief: dict[str, Any], angle: dict[str, Any]
) -> dict[str, Any]:
    claims = brief.get("verified_claims") or []
    claims_text = (
        "; ".join(c["text"] for c in claims) if claims else "(none supplied)"
    )

    base_prompt = prompts.render(
        "creative_spec",
        angle_title=angle["title"],
        angle_hook=angle["hook"],
        angle_insight=angle["audience_insight"],
        angle_visual=angle["visual_direction"],
        angle_rationale=angle["rationale"],
        product_name=brief["product_name"],
        product_description=brief["product_description"],
        target_audience=brief["target_audience"],
        campaign_objective=brief["campaign_objective"],
        tone=brief["tone"],
        call_to_action=brief["call_to_action"],
        verified_claims=claims_text,
        has_reference_image="yes" if brief.get("reference_image_id") else "no",
        video_target_seconds=ctx.settings.video_target_seconds,
    )

    system = (
        "You are an art director. Output only a JSON object matching the provided "
        "schema. Every claim in the copy must be supported by the supplied product "
        "facts or verified claims."
    )

    async with session_scope() as session:
        version = await SpecRepository(session).next_version(ctx.campaign_id)

    correction: str | None = None
    last_error = ""

    for attempt in range(MAX_REPAIRS + 1):
        prompt = base_prompt if correction is None else f"{base_prompt}\n\n{correction}"
        raw = await _call_model(ctx, system=system, user=prompt, attempt=attempt)

        try:
            draft = parse_json_response(raw)
            spec = CampaignSpec.model_validate(
                _assemble(draft, ctx=ctx, brief=brief, angle=angle, version=version)
            )
        except (ValidationError, MalformedOutputError) as exc:
            last_error = _readable(exc)
            log.warning("spec.validation_failed", attempt=attempt, error=last_error[:400])
            if attempt == MAX_REPAIRS:
                break
            correction = (
                "Your previous response was rejected:\n\n"
                f"{last_error}\n\n"
                "Produce a corrected JSON object. Remember: no percentages, "
                "certifications, discounts, superlatives or health outcomes in any "
                "copy field, and the video beats must total between 6 and 10 seconds."
            )
            continue

        log.info(
            "spec.validated",
            attempt=attempt,
            spec_id=spec.spec_id,
            version=spec.version,
            video_s=spec.video.total_seconds,
        )
        return spec.model_dump(mode="json")

    raise MalformedOutputError(
        f"creative spec failed validation after {MAX_REPAIRS + 1} attempts: {last_error}"
    )


def _assemble(
    draft: dict[str, Any],
    *,
    ctx: RunContext,
    brief: dict[str, Any],
    angle: dict[str, Any],
    version: int,
) -> dict[str, Any]:
    """Merge model output with facts the model is not allowed to decide.
    """
    product = draft.get("product") or {}
    return {
        "spec_id": f"spec_{secrets.token_hex(8)}",
        "version": version,
        "campaign_id": ctx.campaign_id,
        "selected_angle_id": angle["id"],
        "hook": draft.get("hook") or angle["hook"],
        "headline": draft.get("headline", ""),
        "subhead": draft.get("subhead"),
        "cta_text": brief["call_to_action"],
        "product": {
            "name": brief["product_name"],
            "form_factor": product.get("form_factor", ""),
            "packaging_description": product.get("packaging_description", ""),
            "reference_image_id": brief.get("reference_image_id"),
        },
        "scene": draft.get("scene") or {},
        "palette": draft.get("palette") or {},
        "composition": draft.get("composition") or {},
        "video": draft.get("video") or {},
    }


async def _call_model(ctx: RunContext, *, system: str, user: str, attempt: int) -> str:
    call_id = f"spec_{uuid.uuid4().hex[:16]}"
    await ledger_dispatch(
        ctx,
        call_id=call_id,
        stage=StageName.BUILD_SPEC.value,
        provider=ctx.providers.llm.name,
        model=getattr(ctx.providers.llm, "model", "unknown"),
        operation=f"complete.attempt{attempt}",
    )
    started = time.monotonic()
    try:
        result = await retrying(
            ctx,
            "spec:build",
            lambda: ctx.providers.llm.complete(
            system=system,
            user=user,
            json_schema=CAMPAIGN_SPEC_SCHEMA,
            temperature=0.6,
                max_tokens=2500,
            ),
        )
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
        prompt_tokens=result.usage.prompt_tokens,
        completion_tokens=result.usage.completion_tokens,
        est_cost_usd=estimate_chat_cost_usd(
            model=result.model,
            prompt_tokens=result.usage.prompt_tokens,
            completion_tokens=result.usage.completion_tokens,
        ),
    )
    return result.text


def _readable(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return "\n".join(
            f"- {'.'.join(str(p) for p in err['loc'])}: {err['msg']}"
            for err in exc.errors()[:8]
        )
    return str(exc)
