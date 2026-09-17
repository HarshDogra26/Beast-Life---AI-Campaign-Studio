"""Bridge between the MCP research server and the agent's tool loop.
"""

from __future__ import annotations
import json
from dataclasses import dataclass, field
from typing import Any
from fastmcp import Client
from ..domain.enums import ToolName
from ..logging import get_logger
from ..providers.base import ToolSchema
from mcp_servers.research_server import mcp as research_server

log = get_logger(__name__)


@dataclass
class ToolBudget:
    """Hard ceilings on the research agent's tool use."""

    max_search_calls: int
    max_fetch_calls: int
    searches_used: int = 0
    fetches_used: int = 0
    exhausted_reason: str | None = None

    _limits: dict[str, str] = field(
        default_factory=lambda: {
            ToolName.WEB_SEARCH.value: "search",
            ToolName.FETCH_PAGE.value: "fetch",
        },
        repr=False,
    )

    def check(self, tool: str) -> str | None:
        """Return a refusal message if this call would exceed budget."""
        kind = self._limits.get(tool)
        if kind == "search" and self.searches_used >= self.max_search_calls:
            return (
                f"Search budget exhausted ({self.max_search_calls} calls). "
                "Stop calling tools and summarise the evidence you already have."
            )
        if kind == "fetch" and self.fetches_used >= self.max_fetch_calls:
            return (
                f"Page-fetch budget exhausted ({self.max_fetch_calls} calls). "
                "Stop calling tools and summarise the evidence you already have."
            )
        return None

    def consume(self, tool: str) -> None:
        kind = self._limits.get(tool)
        if kind == "search":
            self.searches_used += 1
        elif kind == "fetch":
            self.fetches_used += 1

    @property
    def spent(self) -> bool:
        return (
            self.searches_used >= self.max_search_calls
            and self.fetches_used >= self.max_fetch_calls
        )


class ResearchToolClient:
    """Thin MCP client that also enforces the budget.
    """

    def __init__(self, transport: Any, *, budget: ToolBudget, timeout_s: float = 45.0) -> None:
        self._transport = transport
        self._budget = budget
        self._timeout = timeout_s
        self._client: Client | None = None
        self._schemas: list[ToolSchema] = []

    @property
    def budget(self) -> ToolBudget:
        return self._budget

    @property
    def schemas(self) -> list[ToolSchema]:
        return self._schemas

    async def __aenter__(self) -> ResearchToolClient:
        self._client = Client(self._transport, timeout=self._timeout)
        await self._client.__aenter__()
        tools = await self._client.list_tools()
        self._schemas = [
            ToolSchema(
                name=t.name,
                description=t.description or "",
                parameters=t.inputSchema or {"type": "object", "properties": {}},
            )
            for t in tools
        ]
        log.info("mcp.connected", tools=[s.name for s in self._schemas])
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        if self._client is not None:
            await self._client.__aexit__(*exc_info)
            self._client = None

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Invoke an MCP tool, returning a plain dict for the agent transcript."""
        if refusal := self._budget.check(tool):
            self._budget.exhausted_reason = refusal
            log.info("tool.budget_exhausted", tool=tool)
            return {"kind": "budget_exhausted", "tool": tool, "error": refusal}

        if self._client is None:
            raise RuntimeError("ResearchToolClient used outside its context manager")

        self._budget.consume(tool)
        try:
            result = await self._client.call_tool(tool, arguments, raise_on_error=False)
        except Exception as exc:
            log.warning("tool.transport_error", tool=tool, error=str(exc)[:300])
            return {"kind": "error", "tool": tool, "error": f"tool call failed: {exc}"}

        payload = _result_payload(result)
        if getattr(result, "is_error", False):
            payload.setdefault("kind", "error")
            payload.setdefault("tool", tool)
        return payload


def resolve_transport(settings: Any) -> Any:
    """Pick the MCP transport: the server object in-process, or its URL.
    """
    if not settings.mcp_inprocess:
        return settings.mcp_research_url
    return research_server


def _result_payload(result: Any) -> dict[str, Any]:
    """Normalise a ``CallToolResult`` into a dict.
    """
    for attr in ("data", "structured_content"):
        value = getattr(result, attr, None)
        if isinstance(value, dict):
            return value

    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"kind": "text", "text": text}
        if isinstance(parsed, dict):
            return parsed
        return {"kind": "text", "value": parsed}

    return {"kind": "empty"}
