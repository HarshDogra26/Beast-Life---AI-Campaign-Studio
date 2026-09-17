"""Error handling discovered against the real Azure API.

Every case here reproduces something a live deployment actually did, which the
fixture providers could never have surfaced:

* ``gpt-image-2.5-sunburst`` rejects ``input_fidelity`` with a 400, contradicting
  Microsoft's published capability table.
* A 404 for a missing deployment carries a message telling you to wait five
  minutes, which is misleading when the real cause is a name mismatch.
* When one stage of a concurrent pair fails, LangGraph cancels its sibling, and
  ``CancelledError`` must not be laundered into a provider failure.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.config import get_settings
from app.domain.enums import StageName, StageStatus
from app.graph.context import run_stage
from app.providers.azure_image import AzureImageProvider
from app.providers.base import (
    DeploymentNotFoundError,
    ProviderTimeoutError,
    UnsupportedParameterError,
)
from app.providers.capabilities import resolve_capabilities
from app.providers.retry import with_retry
from app.storage.db import session_scope
from app.storage.repo import StageRepository

PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)

#: Azure's verbatim 400 body for the unsupported parameter.
UNSUPPORTED_BODY = {
    "error": {
        "code": "BadRequest",
        "message": (
            "The model 'gpt-image-2.5-sunburst' does not support the "
            "'input_fidelity' parameter."
        ),
    }
}

#: Azure's verbatim 404 body for a missing deployment.
NOT_FOUND_BODY = {
    "error": {
        "code": "DeploymentNotFound",
        "message": (
            "The API deployment for this resource does not exist. If you created "
            "the deployment within the last 5 minutes, please wait a moment and "
            "try again."
        ),
    }
}


def _provider(monkeypatch, handler) -> AzureImageProvider:
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.openai.azure.com")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_OPENAI_IMAGE_DEPLOYMENT", "gpt-image-2.5-sunburst")
    monkeypatch.setenv("AZURE_OPENAI_IMAGE_MODEL", "gpt-image-2.5-sunburst")
    get_settings.cache_clear()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return AzureImageProvider(get_settings(), client)


# --------------------------------------------------------------------------
# input_fidelity
# --------------------------------------------------------------------------


def test_registry_reflects_live_behaviour_not_the_published_table():
    """Live evidence overrides the docs: sunburst rejected input_fidelity."""
    assert resolve_capabilities("gpt-image-2.5-sunburst").supports_input_fidelity is False


async def test_edit_drops_input_fidelity_and_succeeds_on_retry(monkeypatch):
    """The campaign must survive an optional parameter being unsupported."""
    seen: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode("latin-1")
        sent = "input_fidelity" in body
        seen.append(sent)
        if sent:
            return httpx.Response(400, json=UNSUPPORTED_BODY)
        import base64

        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(PNG_1PX).decode()}]}
        )

    provider = _provider(monkeypatch, handler)
    # Force the parameter on, as a stale registry entry would.
    provider._input_fidelity_supported = True

    result = await provider.edit(
        prompt="reframe",
        size="1088x1088",
        quality="high",
        images=[("master.png", PNG_1PX, "image/png")],
        input_fidelity="high",
    )

    assert result.data == PNG_1PX
    assert seen == [True, False], "should retry exactly once, without the parameter"
    assert provider._input_fidelity_supported is False, "the rejection must be remembered"


async def test_second_edit_does_not_resend_the_rejected_parameter(monkeypatch):
    """Remembering it avoids paying for a doomed round-trip on every asset."""
    attempts: list[bool] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent = "input_fidelity" in request.content.decode("latin-1")
        attempts.append(sent)
        if sent:
            return httpx.Response(400, json=UNSUPPORTED_BODY)
        import base64

        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(PNG_1PX).decode()}]}
        )

    provider = _provider(monkeypatch, handler)
    provider._input_fidelity_supported = True

    for _ in range(2):
        await provider.edit(
            prompt="reframe",
            size="1088x1088",
            quality="high",
            images=[("master.png", PNG_1PX, "image/png")],
            input_fidelity="high",
        )

    assert attempts == [True, False, False], f"unexpected attempt pattern: {attempts}"


async def test_unrelated_400_is_not_swallowed(monkeypatch):
    """Only the specific parameter rejection degrades; other 400s still fail."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"code": "BadRequest", "message": "prompt was empty"}}
        )

    provider = _provider(monkeypatch, handler)
    with pytest.raises(Exception) as exc:
        await provider.edit(
            prompt="",
            size="1088x1088",
            quality="high",
            images=[("m.png", PNG_1PX, "image/png")],
        )
    assert not isinstance(exc.value, UnsupportedParameterError)


# --------------------------------------------------------------------------
# missing deployment
# --------------------------------------------------------------------------


async def test_missing_deployment_names_the_setting_to_fix(monkeypatch):
    """Azure's 'wait 5 minutes' text is unhelpful when the name is simply wrong."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json=NOT_FOUND_BODY)

    provider = _provider(monkeypatch, handler)
    with pytest.raises(DeploymentNotFoundError) as exc:
        await provider.generate(prompt="x", size="1024x1024", quality="high")

    message = str(exc.value)
    assert "gpt-image-2.5-sunburst" in message
    assert "AZURE_OPENAI_IMAGE_DEPLOYMENT" in message
    assert exc.value.retryable is False, "a missing deployment will not appear on retry"


# --------------------------------------------------------------------------
# concurrent cancellation
# --------------------------------------------------------------------------


async def test_cancellation_is_not_reported_as_a_provider_error():
    """`with_retry` must propagate cancellation untouched, not classify it."""
    async def cancelled() -> str:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await with_retry(cancelled, attempts=3, base_delay_s=0.01, label="t")


async def test_cancelled_sibling_stage_is_marked_interrupted_not_failed(context):
    """When a concurrent sibling fails, this stage did nothing wrong.

    Recording it as FAILED would point the user at the wrong stage; INTERRUPTED
    is honest and is already retryable in the UI.
    """
    async def cancelled_work() -> dict:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await run_stage(
            context, StageName.RENDER_SQUARE, inputs={"v": 1}, work=cancelled_work
        )

    async with session_scope() as session:
        row = await StageRepository(session).get(
            context.campaign_id, StageName.RENDER_SQUARE
        )

    assert row.status == StageStatus.INTERRUPTED
    assert row.error_kind == "cancelled"
    assert "concurrent stage failed" in row.error


async def test_cancelled_image_call_settles_the_ledger(context):
    """A cancelled call must not linger as 'dispatched'.

    ``dispatched`` means "we have no idea what happened, it may have been
    billed" — the strongest warning the ledger can raise. Cancellation is a
    weaker, known state, and reporting it as the stronger one cries wolf.
    Regression test: ``except Exception`` does not catch ``CancelledError``.
    """
    from app.graph.nodes.images import _invoke
    from app.storage.repo import LedgerRepository

    async def cancelled_call():
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await _invoke(
            context,
            stage=StageName.RENDER_SQUARE,
            operation="edits",
            call=cancelled_call,
        )

    async with session_scope() as session:
        repo = LedgerRepository(session)
        calls = await repo.list(context.campaign_id)
        unsettled = await repo.unsettled(context.campaign_id)

    assert len(calls) == 1
    assert calls[0].status == "cancelled"
    assert unsettled == [], "a cancelled call is known, not an unexplained charge"


async def test_a_real_failure_is_still_marked_failed(context):
    """Guard against the cancellation branch swallowing genuine faults."""
    async def failing_work() -> dict:
        raise ProviderTimeoutError("upstream timed out")

    with pytest.raises(ProviderTimeoutError):
        await run_stage(
            context, StageName.RENDER_VERTICAL, inputs={"v": 1}, work=failing_work
        )

    async with session_scope() as session:
        row = await StageRepository(session).get(
            context.campaign_id, StageName.RENDER_VERTICAL
        )

    assert row.status == StageStatus.FAILED
    assert row.error_kind == "timeout"
