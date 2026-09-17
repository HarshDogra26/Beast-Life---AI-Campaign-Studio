"""Regression tests for a live failure: research that read nothing.

Observed against Azure: the agent ran three searches, never called `fetch_page`,
then produced a text-only turn narrating its intent. The loop treated any
tool-free turn as completion, so the run ended with zero sources — and the
failure surfaced three stages later as "angle a1 cites unknown source ids
['s0']", pointing at the wrong stage entirely.

Three things were wrong and each is covered here:

1. A text-only turn ended research even with no evidence gathered.
2. Zero sources were passed downstream, where the contract is unsatisfiable —
   an angle must cite a real source id, so no model output could ever validate.
3. The trace was only persisted after synthesis succeeded, so the evidence
   needed to diagnose the failure was discarded.
"""

from __future__ import annotations

import pytest

from app.domain.enums import StageName
from app.graph.nodes.research import MAX_NUDGES, research_node
from app.providers.base import (
    ChatTurn,
    LLMUsage,
    NoSourcesError,
    ProviderTimeoutError,
    ToolCall,
)
from app.storage.db import session_scope
from app.storage.repo import ResearchRepository, StageRepository

from .conftest import sample_brief


class ScriptedLLM:
    """Chat model driven by a fixed script of turns, with a fallback."""

    name = "scripted"
    model = "scripted"

    def __init__(self, turns: list[ChatTurn], fallback: ChatTurn | None = None) -> None:
        self._turns = list(turns)
        self._fallback = fallback or _text("Nothing further.")
        self.calls = 0

    async def chat(self, **_: object) -> ChatTurn:
        self.calls += 1
        return self._turns.pop(0) if self._turns else self._fallback

    async def complete(self, **_: object):  # pragma: no cover - unused here
        raise AssertionError("complete() should not be called by the research loop")


def _text(content: str) -> ChatTurn:
    return ChatTurn(content=content, tool_calls=[], finish_reason="stop",
                    usage=LLMUsage(1, 1), model="scripted")


def _search(query: str) -> ChatTurn:
    return ChatTurn(
        content=f"Searching for {query}.",
        tool_calls=[ToolCall(id="c1", name="web_search",
                             arguments={"query": query, "max_results": 3})],
        finish_reason="tool_calls",
        usage=LLMUsage(1, 1),
        model="scripted",
    )


def _fetch(url: str) -> ChatTurn:
    return ChatTurn(
        content=f"Reading {url}.",
        tool_calls=[ToolCall(id="c2", name="fetch_page", arguments={"url": url})],
        finish_reason="tool_calls",
        usage=LLMUsage(1, 1),
        model="scripted",
    )


PAGE = "https://example-research.org/reports/post-workout-routines"


async def _run(context, turns, fallback=None) -> dict:
    context.providers.llm = ScriptedLLM(turns, fallback)
    result = await research_node({"brief": sample_brief()}, context)
    return result["research"]


# --------------------------------------------------------------------------
# 1. the text-only turn no longer ends research prematurely
# --------------------------------------------------------------------------


async def test_agent_is_nudged_to_fetch_when_it_only_searched(context):
    """The exact live failure: search, search, then narrate without fetching."""
    report = await _run(
        context,
        [
            _search("gym habits"),
            _search("gym motivations"),
            _text("Several results look highly relevant, especially the first one."),
            _fetch(PAGE),          # the agent complies after the nudge
            _text("That is enough evidence."),
        ],
    )

    assert len(report["sources"]) == 1, "the nudge should have rescued the run"
    assert any(
        "prompted to fetch" in s["decision_summary"] for s in report["steps"]
    ), "the nudge must be visible in the trace, not a hidden correction"


async def test_nudging_is_bounded(context):
    """A model that never fetches must still terminate, not loop forever."""
    with pytest.raises(NoSourcesError):
        await _run(
            context,
            [_search("gym habits")],
            fallback=_text("I will read a page next."),  # never actually does
        )

    async with session_scope() as session:
        report = await ResearchRepository(session).get(context.campaign_id)

    nudged = [s for s in report["steps"] if "prompted to fetch" in s["decision_summary"]]
    assert len(nudged) <= MAX_NUDGES, f"nudged {len(nudged)} times, limit is {MAX_NUDGES}"


async def test_no_nudge_once_evidence_exists(context):
    """A normal finish must not be second-guessed."""
    report = await _run(
        context,
        [_search("gym habits"), _fetch(PAGE), _text("I have what I need.")],
    )

    assert len(report["sources"]) == 1
    assert not any(
        "prompted to fetch" in s["decision_summary"] for s in report["steps"]
    )


# --------------------------------------------------------------------------
# 2. zero sources fails here, not three stages later
# --------------------------------------------------------------------------


async def test_zero_sources_fails_the_research_stage(context):
    """Blaming synthesis for empty research points at the wrong stage.

    With no sources, `insight_source_ids` (min length 1) cannot be satisfied by
    any output, so the repair loop is guaranteed to fail — three paid attempts
    for an impossible constraint.
    """
    with pytest.raises(NoSourcesError) as exc:
        await _run(context, [_search("gym habits")], fallback=_text("Done."))

    message = str(exc.value)
    assert "without reading any pages" in message
    assert "searches" in message and "page fetches" in message, "must report budget used"

    async with session_scope() as session:
        row = await StageRepository(session).get(context.campaign_id, StageName.RESEARCH)

    assert row.status == "failed"
    assert row.error_kind == "no_sources"


async def test_no_sources_error_is_not_retried_automatically():
    """Re-running the same queries is unlikely to help; the user decides."""
    assert NoSourcesError("x").retryable is False


# --------------------------------------------------------------------------
# 3. the trace survives failure
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# 4. LLM calls get the same retry policy as image calls
# --------------------------------------------------------------------------


class FlakyLLM(ScriptedLLM):
    """Times out the first N chat calls, then behaves normally."""

    def __init__(self, turns: list[ChatTurn], *, failures: int) -> None:
        super().__init__(turns)
        self._remaining = failures
        self.attempts = 0

    async def chat(self, **kwargs: object) -> ChatTurn:
        self.attempts += 1
        if self._remaining > 0:
            self._remaining -= 1
            raise ProviderTimeoutError("chat request exceeded LLM_TIMEOUT_S=120s")
        return await super().chat(**kwargs)


async def test_transient_chat_timeout_is_retried(context):
    """A transient timeout must not kill the research stage.

    Regression: the retry policy was wired into image calls only, so an
    identical transient fault was fatal for chat and recoverable for images.
    """
    context.providers.llm = FlakyLLM(
        [_search("gym habits"), _fetch(PAGE), _text("Done.")], failures=2
    )
    result = await research_node({"brief": sample_brief()}, context)

    assert len(result["research"]["sources"]) == 1
    assert context.providers.llm.attempts == 5, (
        "2 failed attempts + 3 successful turns; "
        f"got {context.providers.llm.attempts}"
    )


async def test_chat_timeout_still_fails_once_retries_are_exhausted(context):
    """Retrying is bounded — a persistent outage surfaces as a failed stage."""
    context.providers.llm = FlakyLLM([_text("unreachable")], failures=99)

    with pytest.raises(ProviderTimeoutError):
        await research_node({"brief": sample_brief()}, context)

    assert context.providers.llm.attempts == context.settings.max_retries

    async with session_scope() as session:
        row = await StageRepository(session).get(context.campaign_id, StageName.RESEARCH)
    assert row.status == "failed"
    assert row.error_kind == "timeout"


async def test_trace_is_persisted_even_when_research_fails(context):
    """The trace is most valuable exactly when the run went wrong.

    Previously it was saved only after synthesis succeeded, so diagnosing this
    failure required reading the raw stage_runs row by hand.
    """
    with pytest.raises(NoSourcesError):
        await _run(
            context,
            [_search("gym habits"), _search("gym motivations")],
            fallback=_text("Stopping."),
        )

    async with session_scope() as session:
        report = await ResearchRepository(session).get(context.campaign_id)

    assert report is not None, "the trace must survive the failure"
    assert report["usage"]["search_calls"] >= 2
    assert report["steps"], "every turn should be recorded"
    assert report["coverage_gap"], "an empty run must explain itself"
