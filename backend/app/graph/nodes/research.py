"""The research agent: a bounded, fully observable tool loop.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any
from ... import prompts
from ...domain.enums import StageName, ToolName
from ...domain.research import SourceDoc, detect_injection
from ...logging import get_logger
from ...providers.base import ChatTurn, NoSourcesError, ToolCall
from ...providers.pricing import estimate_chat_cost_usd
from ...security.sanitize import make_excerpt
from ...storage.db import session_scope
from ...storage.repo import ResearchRepository
from ..context import RunContext, ledger_dispatch, ledger_settle, retrying, run_stage
from ..research_tools import ResearchToolClient, ToolBudget, resolve_transport
from ..state import CampaignState

log = get_logger(__name__)

MAX_TURNS_HEADROOM = 3

#: How many times the agent may be prompted to actually open a page when it
#: has searched but read nothing. Bounded so a model that refuses to fetch
#: still terminates instead of looping.
MAX_NUDGES = 2


async def research_node(state: CampaignState, ctx: RunContext) -> dict[str, Any]:
    brief = state["brief"]

    async def work() -> dict[str, Any]:
        report = await _run_research(ctx, brief)

        # Persist the trace before any success/failure decision. This is the
        # only record of what the agent actually did, and it is most valuable
        # precisely when the run went wrong.
        async with session_scope() as session:
            await ResearchRepository(session).save(
                campaign_id=ctx.campaign_id, report=report
            )

        if not report["sources"]:
            raise NoSourcesError(
                "Research finished without reading any pages, so there is no evidence "
                "to cite and no angle can be produced. "
                f"Budget used: {report['usage']['search_calls']}/"
                f"{report['budget']['max_search_calls']} searches, "
                f"{report['usage']['fetch_calls']}/"
                f"{report['budget']['max_fetch_calls']} page fetches. "
                "Retry this stage, or widen the brief's target audience if searches "
                "are returning nothing relevant."
            )
        return report

    output = await run_stage(
        ctx,
        StageName.RESEARCH,
        inputs={
            "brief": brief,
            "budget": {
                "search": ctx.settings.max_search_calls,
                "fetch": ctx.settings.max_fetch_calls,
            },
            "mode": ctx.providers.mode.value,
        },
        work=work,
    )
    return {"research": output, "notes": [f"research: {len((output or {}).get('sources', []))} sources"]}


async def _run_research(ctx: RunContext, brief: dict[str, Any]) -> dict[str, Any]:
    settings = ctx.settings
    budget = ToolBudget(
        max_search_calls=settings.max_search_calls,
        max_fetch_calls=settings.max_fetch_calls,
    )

    system = prompts.render(
        "research_agent",
        min_sources=settings.min_sources,
        max_search_calls=settings.max_search_calls,
        max_fetch_calls=settings.max_fetch_calls,
        wall_clock_s=int(settings.research_wall_clock_s),
        product_name=brief["product_name"],
        product_description=brief["product_description"],
        target_audience=brief["target_audience"],
        campaign_objective=brief["campaign_objective"],
        tone=brief["tone"],
    )

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {
            "role": "user",
            "content": (
                "Begin your research. Explain your first decision, then call a tool."
            ),
        },
    ]

    steps: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    fetched_urls: set[str] = set()
    started = time.monotonic()
    nudges = 0
    max_turns = (
        settings.max_search_calls
        + settings.max_fetch_calls
        + MAX_TURNS_HEADROOM
        + MAX_NUDGES
    )
    stop_reason = "agent_finished"

    async with ResearchToolClient(resolve_transport(settings), budget=budget) as tools:
        for turn_index in range(max_turns):
            elapsed = time.monotonic() - started
            if elapsed > settings.research_wall_clock_s:
                stop_reason = f"wall clock budget of {settings.research_wall_clock_s}s exceeded"
                log.warning("research.wall_clock_exceeded", elapsed_s=round(elapsed, 1))
                break

            turn = await _chat_turn(ctx, messages, tools, turn_index)
            summary = (turn.content or "").strip() or "(no decision summary provided)"
            step: dict[str, Any] = {
                "index": turn_index,
                "decision_summary": summary[:600],
                "tool_calls": [],
            }

            if not turn.wants_tools:
                # A turn with no tool call normally means the agent is done. But
                # a model that has only searched, never opened a page, and then
                # narrates its intent would end the run with zero evidence. When
                # nothing has been read and fetch budget remains, prompt it once
                # rather than accepting an empty result.
                if (
                    not sources
                    and nudges < MAX_NUDGES
                    and budget.fetches_used < settings.max_fetch_calls
                ):
                    nudges += 1
                    step["decision_summary"] = (
                        f"{summary[:400]} [no page was opened; agent prompted to fetch]"
                    )
                    steps.append(step)
                    messages.append({"role": "assistant", "content": turn.content or ""})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "You have not read any pages yet, so you have no evidence "
                                "to cite and no angle can be produced. Call fetch_page on "
                                "the most promising URL from your search results now. If "
                                "none of the results are usable, say so explicitly and "
                                "run a different search instead."
                            ),
                        }
                    )
                    log.warning(
                        "research.nudged_to_fetch", turn=turn_index, nudge=nudges
                    )
                    continue

                steps.append(step)
                log.info("research.agent_finished", turn=turn_index, sources=len(sources))
                break

            messages.append(_assistant_message(turn))

            for call in turn.tool_calls:
                record, payload = await _execute_tool(tools, call)
                step["tool_calls"].append(record)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(_for_model(payload))[:8000],
                    }
                )
                if payload.get("kind") == "page":
                    _absorb_page(payload, sources, fetched_urls)

            steps.append(step)

            if budget.spent:
                stop_reason = "tool budget exhausted"
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Your tool budget is now exhausted. Do not call any more "
                            "tools. Summarise what your sources support."
                        ),
                    }
                )
        else:
            stop_reason = f"turn limit of {max_turns} reached"

    elapsed = round(time.monotonic() - started, 2)
    coverage_gap = _coverage_gap(sources, settings.min_sources, stop_reason, budget)

    log.info(
        "research.complete",
        sources=len(sources),
        searches=budget.searches_used,
        fetches=budget.fetches_used,
        elapsed_s=elapsed,
        stop_reason=stop_reason,
    )

    return {
        "sources": sources,
        "steps": steps,
        "budget": {
            "max_search_calls": settings.max_search_calls,
            "max_fetch_calls": settings.max_fetch_calls,
            "wall_clock_s": settings.research_wall_clock_s,
        },
        "usage": {
            "search_calls": budget.searches_used,
            "fetch_calls": budget.fetches_used,
            "elapsed_s": elapsed,
            "exhausted_reason": budget.exhausted_reason,
        },
        "stop_reason": stop_reason,
        "coverage_gap": coverage_gap,
        "provider_mode": ctx.providers.mode.value,
    }


async def _chat_turn(
    ctx: RunContext, messages: list[dict[str, Any]], tools: ResearchToolClient, turn: int
) -> ChatTurn:
    call_id = f"chat_{uuid.uuid4().hex[:16]}"
    await ledger_dispatch(
        ctx,
        call_id=call_id,
        stage=StageName.RESEARCH.value,
        provider=ctx.providers.llm.name,
        model=getattr(ctx.providers.llm, "model", "unknown"),
        operation="chat",
    )
    started = time.monotonic()
    try:
        result = await retrying(
            ctx,
            f"research:turn{turn}",
            lambda: ctx.providers.llm.chat(
                messages=messages, tools=tools.schemas, temperature=0.4
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
    log.info("research.turn", turn=turn, tool_calls=[c.name for c in result.tool_calls])
    return result


async def _execute_tool(
    tools: ResearchToolClient, call: ToolCall
) -> tuple[dict[str, Any], dict[str, Any]]:
    started = time.monotonic()
    payload = await tools.call(call.name, call.arguments)
    latency_ms = int((time.monotonic() - started) * 1000)

    ok = payload.get("kind") not in {"error", "budget_exhausted"}
    record = {
        "tool": call.name if call.name in set(ToolName) else ToolName.WEB_SEARCH.value,
        "arguments": call.arguments,
        "started_at": datetime.now(UTC).isoformat(),
        "latency_ms": latency_ms,
        "ok": ok,
        "result_summary": _summarise(payload)[:600],
        "error": payload.get("error") if not ok else None,
        "credits_spent": int(payload.get("credits") or 0),
    }
    log.info("tool.called", tool=call.name, ok=ok, latency_ms=latency_ms)
    return record, payload


def _assistant_message(turn: ChatTurn) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": turn.content or "",
        "tool_calls": [
            {
                "id": c.id,
                "type": "function",
                "function": {"name": c.name, "arguments": json.dumps(c.arguments)},
            }
            for c in turn.tool_calls
        ],
    }


def _for_model(payload: dict[str, Any]) -> dict[str, Any]:
    """Trim a tool payload before it re-enters the prompt.

    Full page text is kept in ``sources`` for the synthesis step; echoing it back
    through every subsequent turn would multiply cost for no benefit.
    """
    if payload.get("kind") != "page":
        return payload
    trimmed = dict(payload)
    trimmed["text"] = (payload.get("text") or "")[:2000]
    return trimmed


def _absorb_page(
    payload: dict[str, Any], sources: list[dict[str, Any]], seen: set[str]
) -> None:
    """Turn a successful fetch into a validated SourceDoc."""
    url = payload.get("url")
    if not url or url in seen:
        return
    seen.add(url)

    text = payload.get("text") or ""
    flags = payload.get("injection_flags") or detect_injection(text)
    if flags:
        log.warning("research.injection_flagged", url=url, flags=flags)

    try:
        doc = SourceDoc(
            id=f"s{len(sources) + 1}",
            url=url,
            title=payload.get("title") or url,
            accessed_at=payload.get("fetched_at") or datetime.now(UTC),
            excerpt=payload.get("excerpt") or make_excerpt(text),
            summary=(text[:780] or "(no extractable text)"),
            injection_flags=flags,
        )
    except Exception as exc:
        log.warning("research.source_rejected", url=url, error=str(exc)[:200])
        return

    record = doc.model_dump(mode="json")
    record["text"] = text
    sources.append(record)


def _summarise(payload: dict[str, Any]) -> str:
    kind = payload.get("kind")
    if kind == "search_results":
        hosts = ", ".join(
            sorted({_host(r.get("url", "")) for r in payload.get("results", [])})
        )
        return f"{payload.get('result_count', 0)} results from: {hosts or 'none'}"
    if kind == "page":
        flags = payload.get("injection_flags") or []
        note = f" [flagged: {', '.join(flags)}]" if flags else ""
        return f"read {payload.get('char_count', 0)} chars from {payload.get('url')}{note}"
    if kind in {"error", "budget_exhausted"}:
        return f"{kind}: {payload.get('error', '')}"
    return kind or "unknown result"


def _host(url: str) -> str:
    from urllib.parse import urlparse

    return urlparse(url).netloc or url


def _coverage_gap(
    sources: list[dict[str, Any]], minimum: int, stop_reason: str, budget: ToolBudget
) -> str | None:
    """Report an evidence shortfall rather than papering over it."""
    if len(sources) >= minimum:
        return None
    return (
        f"Only {len(sources)} source(s) were successfully read, below the target of "
        f"{minimum}. Research stopped because: {stop_reason}. "
        f"Budget used: {budget.searches_used}/{budget.max_search_calls} searches, "
        f"{budget.fetches_used}/{budget.max_fetch_calls} fetches. "
        "The angles below rest on a thinner evidence base than intended."
    )
