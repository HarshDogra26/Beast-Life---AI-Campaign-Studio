"""Reuse of successful stage outputs.

The requirement is that a failed image or video stage can be retried *without*
rerunning successful stages. The load-bearing evidence is not that the status
column says "reused" — it is that the provider spy counters do not move. These
tests assert the counters.

Reuse is keyed on an input fingerprint as well as status, which is what keeps it
correct rather than merely fast: if an upstream output changes, the downstream
cache must miss.
"""

from __future__ import annotations

from app.domain.enums import StageName, StageStatus
from app.graph.context import run_stage
from app.graph.nodes.images import master_scene_node, render_square_node
from app.graph.nodes.video import render_video_node
from app.storage.db import session_scope
from app.storage.repo import StageRepository, fingerprint

from .test_contracts import _spec


async def test_completed_stage_with_same_inputs_is_not_re_executed(context):
    runs = 0

    async def work() -> dict:
        nonlocal runs
        runs += 1
        return {"value": runs}

    first = await run_stage(context, StageName.BUILD_SPEC, inputs={"a": 1}, work=work)
    second = await run_stage(context, StageName.BUILD_SPEC, inputs={"a": 1}, work=work)

    assert runs == 1, "the second call must reuse the stored output"
    assert first == second == {"value": 1}

    async with session_scope() as session:
        row = await StageRepository(session).get(context.campaign_id, StageName.BUILD_SPEC)
    assert row.status == StageStatus.REUSED


async def test_changed_inputs_invalidate_the_cache(context):
    """Status alone would wrongly reuse a render built from an edited spec."""
    runs = 0

    async def work() -> dict:
        nonlocal runs
        runs += 1
        return {"value": runs}

    await run_stage(context, StageName.BUILD_SPEC, inputs={"a": 1}, work=work)
    await run_stage(context, StageName.BUILD_SPEC, inputs={"a": 2}, work=work)

    assert runs == 2, "a different fingerprint must miss the cache"


async def test_fingerprint_ignores_key_order():
    """Dict ordering must never cause an avoidable paid provider call."""
    assert fingerprint({"a": 1, "b": 2}) == fingerprint({"b": 2, "a": 1})
    assert fingerprint({"a": 1}) != fingerprint({"a": 2})


async def test_retry_of_video_reuses_every_upstream_image_stage(context):
    """The headline guarantee, measured in provider calls.

    Generate the master scene and the square ad, fail the video, then retry only
    the video. The image provider must not be touched again.
    """
    image = context.providers.image
    spec = _spec(campaign_id=context.campaign_id)
    state = {"spec": spec, "assets": {}}

    master = await master_scene_node(state, context)
    state["assets"].update(master["assets"])
    assert image.total_calls == 1, "master scene is one generation"

    square = await render_square_node(state, context)
    state["assets"].update(square["assets"])
    assert image.total_calls == 2, "square ad is one edit of the master scene"

    calls_before_video = image.total_calls

    # Fail the video stage.
    async def failing_video() -> dict:
        raise RuntimeError("ffmpeg fell over")

    try:
        await run_stage(
            context, StageName.RENDER_VIDEO, inputs={"spec": spec["spec_id"]},
            work=failing_video,
        )
    except RuntimeError:
        pass

    # Retry: reset only the video stage, exactly as the API endpoint does.
    async with session_scope() as session:
        await StageRepository(session).reset_for_retry(
            campaign_id=context.campaign_id, stage=StageName.RENDER_VIDEO
        )

    # Replaying the upstream stages must hit the cache.
    await master_scene_node(state, context)
    await render_square_node(state, context)

    assert image.total_calls == calls_before_video, (
        f"replaying upstream stages spent {image.total_calls - calls_before_video} "
        "extra image call(s); they should have been reused"
    )

    async with session_scope() as session:
        repo = StageRepository(session)
        assert (await repo.get(context.campaign_id, StageName.MASTER_SCENE)).status == StageStatus.REUSED
        assert (await repo.get(context.campaign_id, StageName.RENDER_SQUARE)).status == StageStatus.REUSED
        assert (await repo.get(context.campaign_id, StageName.RENDER_VIDEO)).status == StageStatus.PENDING


async def test_retry_resets_only_the_named_stage(context):
    """Collateral resets would turn a cheap retry into a full rerun."""
    async def work() -> dict:
        return {"ok": True}

    for stage in (StageName.MASTER_SCENE, StageName.RENDER_SQUARE, StageName.RENDER_VERTICAL):
        await run_stage(context, stage, inputs={"v": 1}, work=work)

    async with session_scope() as session:
        await StageRepository(session).reset_for_retry(
            campaign_id=context.campaign_id, stage=StageName.RENDER_SQUARE
        )

    async with session_scope() as session:
        repo = StageRepository(session)
        assert (await repo.get(context.campaign_id, StageName.RENDER_SQUARE)).status == StageStatus.PENDING
        assert (await repo.get(context.campaign_id, StageName.MASTER_SCENE)).status == StageStatus.COMPLETED
        assert (await repo.get(context.campaign_id, StageName.RENDER_VERTICAL)).status == StageStatus.COMPLETED


async def test_reset_clears_the_fingerprint_so_an_explicit_retry_really_runs(context):
    """Without clearing it, a user-requested retry would be short-circuited."""
    runs = 0

    async def work() -> dict:
        nonlocal runs
        runs += 1
        return {"n": runs}

    await run_stage(context, StageName.RENDER_VERTICAL, inputs={"v": 1}, work=work)

    async with session_scope() as session:
        await StageRepository(session).reset_for_retry(
            campaign_id=context.campaign_id, stage=StageName.RENDER_VERTICAL
        )

    await run_stage(context, StageName.RENDER_VERTICAL, inputs={"v": 1}, work=work)
    assert runs == 2, "an explicitly requested retry must re-execute the work"


async def test_video_stage_reuses_its_own_output_on_a_later_replay(context, monkeypatch):
    """A completed video is not re-encoded when the graph replays for another reason."""
    spec = _spec(campaign_id=context.campaign_id)
    state = {"spec": spec, "assets": {}}
    state["assets"].update((await master_scene_node(state, context))["assets"])

    from app.graph.nodes import images as images_module

    vertical = await images_module.render_vertical_node(state, context)
    state["assets"].update(vertical["assets"])

    renders = 0
    real_render = None

    async def counting_render(**kwargs):
        nonlocal renders
        renders += 1
        return await real_render(**kwargs)

    from app.graph.nodes import video as video_module

    real_render = video_module.render_video
    monkeypatch.setattr(video_module, "render_video", counting_render)

    await render_video_node(state, context)
    await render_video_node(state, context)

    assert renders == 1, "the second invocation must reuse the encoded video"
