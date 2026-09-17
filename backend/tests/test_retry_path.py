"""Failure, backoff, and recovery.

Covers the assignment's "one failure-and-retry path" from three angles:

* a transient provider fault that succeeds after backoff;
* a persistent fault that ends FAILED, stays retryable, and leaves the campaign
  readable rather than wedged;
* a terminal fault (content policy) that must **not** be retried, because doing
  so spends money to receive the same answer.
"""

from __future__ import annotations

import time

import pytest

from app.domain.enums import StageName, StageStatus
from app.graph.context import run_stage
from app.providers.base import (
    ContentPolicyError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
)
from app.providers.registry import FailureInjector
from app.providers.retry import with_retry
from app.storage.db import session_scope
from app.storage.repo import StageRepository


# --------------------------------------------------------------------------
# retry policy
# --------------------------------------------------------------------------


async def test_transient_failure_recovers_after_backoff():
    attempts = 0

    async def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise RateLimitError("429 from provider")
        return "ok"

    result = await with_retry(flaky, attempts=3, base_delay_s=0.01, label="test")
    assert result == "ok"
    assert attempts == 3, "should have retried exactly twice before succeeding"


async def test_persistent_transient_failure_exhausts_and_raises():
    attempts = 0

    async def always_fails() -> str:
        nonlocal attempts
        attempts += 1
        raise ProviderTimeoutError("upstream timeout")

    with pytest.raises(ProviderTimeoutError):
        await with_retry(always_fails, attempts=3, base_delay_s=0.01, label="test")
    assert attempts == 3


async def test_terminal_error_is_not_retried():
    """Retrying a content-policy rejection is pure waste: same prompt, same answer."""
    attempts = 0

    async def rejected() -> str:
        nonlocal attempts
        attempts += 1
        raise ContentPolicyError("blocked by content safety")

    with pytest.raises(ContentPolicyError):
        await with_retry(rejected, attempts=5, base_delay_s=0.01, label="test")
    assert attempts == 1, "terminal errors must fail on the first attempt"


async def test_retry_after_header_is_honoured():
    """A server-provided delay beats our own guess."""
    attempts = 0

    async def rate_limited() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RateLimitError("slow down", retry_after_s=0.2)
        return "ok"

    started = time.monotonic()
    assert await with_retry(rate_limited, attempts=2, base_delay_s=0.001, label="t") == "ok"
    assert time.monotonic() - started >= 0.2


# --------------------------------------------------------------------------
# failure injection — reproducible on demand
# --------------------------------------------------------------------------


def test_injector_fires_a_bounded_number_of_times():
    """`stage:429*2` fails twice then lets the work through — the shape needed to
    demonstrate backoff followed by recovery."""
    injector = FailureInjector({"render_square": "429*2"})

    for _ in range(2):
        with pytest.raises(RateLimitError):
            injector.maybe_fail("render_square")

    injector.maybe_fail("render_square")  # third call passes
    injector.maybe_fail("render_vertical")  # unconfigured stage unaffected


def test_injector_without_a_count_fails_every_time():
    injector = FailureInjector({"render_video": "timeout"})
    for _ in range(4):
        with pytest.raises(ProviderTimeoutError):
            injector.maybe_fail("render_video")


def test_injector_ignores_unparseable_configuration():
    """A typo in FAILURE_INJECT must not silently disable real work."""
    injector = FailureInjector({"render_video": "not-a-real-kind", "bad": ""})
    assert not injector.active
    injector.maybe_fail("render_video")


# --------------------------------------------------------------------------
# stage-level failure handling
# --------------------------------------------------------------------------


async def test_failed_stage_is_recorded_with_a_typed_error(context):
    """The UI needs the cause, not a stack trace."""

    async def failing_work() -> dict:
        raise RateLimitError("provider said 429")

    with pytest.raises(RateLimitError):
        await run_stage(
            context, StageName.MASTER_SCENE, inputs={"v": 1}, work=failing_work
        )

    async with session_scope() as session:
        row = await StageRepository(session).get(context.campaign_id, StageName.MASTER_SCENE)

    assert row is not None
    assert row.status == StageStatus.FAILED
    assert row.error_kind == "rate_limit"
    assert "429" in row.error
    assert row.attempt == 1


async def test_retry_after_failure_succeeds_and_clears_the_error(context):
    """The recovery half: reset the stage, run again, end COMPLETED."""
    calls = 0

    async def eventually_works() -> dict:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ProviderTimeoutError("first attempt timed out")
        return {"ok": True}

    with pytest.raises(ProviderTimeoutError):
        await run_stage(context, StageName.RENDER_VIDEO, inputs={"v": 1}, work=eventually_works)

    async with session_scope() as session:
        await StageRepository(session).reset_for_retry(
            campaign_id=context.campaign_id, stage=StageName.RENDER_VIDEO
        )

    output = await run_stage(
        context, StageName.RENDER_VIDEO, inputs={"v": 1}, work=eventually_works
    )
    assert output == {"ok": True}

    async with session_scope() as session:
        row = await StageRepository(session).get(context.campaign_id, StageName.RENDER_VIDEO)

    assert row.status == StageStatus.COMPLETED
    assert row.error is None and row.error_kind is None
    assert row.attempt == 2, "attempt count should reflect both tries"


async def test_injected_failure_fires_before_any_provider_work(context):
    """A reproduced failure must not leave a half-finished artifact behind."""
    context.providers.injector = FailureInjector({"render_square": "timeout"})
    touched = False

    async def work() -> dict:
        nonlocal touched
        touched = True
        return {}

    with pytest.raises(ProviderTimeoutError):
        await run_stage(context, StageName.RENDER_SQUARE, inputs={"v": 1}, work=work)

    assert not touched, "the work body must not run when a failure is injected"


async def test_orphan_sweep_marks_running_stages_from_a_dead_process(context):
    """Interrupted work is detected after restart rather than hanging forever."""
    async with session_scope() as session:
        repo = StageRepository(session)
        await repo.begin_stage(
            campaign_id=context.campaign_id,
            stage=StageName.RENDER_VIDEO,
            input_fingerprint="abc",
            worker_epoch="old-dead-epoch",
        )

    async with session_scope() as session:
        swept = await StageRepository(session).sweep_orphans(current_epoch="new-live-epoch")
    assert swept == 1

    async with session_scope() as session:
        row = await StageRepository(session).get(context.campaign_id, StageName.RENDER_VIDEO)

    assert row.status == StageStatus.INTERRUPTED
    assert row.error_kind == "interrupted_by_restart"


async def test_sweep_does_not_touch_stages_owned_by_the_live_process(context):
    """A stage genuinely running right now must survive the sweep."""
    async with session_scope() as session:
        await StageRepository(session).begin_stage(
            campaign_id=context.campaign_id,
            stage=StageName.RESEARCH,
            input_fingerprint="abc",
            worker_epoch="live-epoch",
        )

    async with session_scope() as session:
        swept = await StageRepository(session).sweep_orphans(current_epoch="live-epoch")
    assert swept == 0

    async with session_scope() as session:
        row = await StageRepository(session).get(context.campaign_id, StageName.RESEARCH)
    assert row.status == StageStatus.RUNNING


async def test_provider_error_taxonomy_declares_retryability():
    """Retry policy is a property of the error, not of the call site."""
    assert RateLimitError("x").retryable
    assert ProviderTimeoutError("x").retryable
    assert not ContentPolicyError("x").retryable
    assert not ProviderError("x").retryable
