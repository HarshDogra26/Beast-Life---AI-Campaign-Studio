"""Web search and page extraction.
"""

from __future__ import annotations
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
import httpx
from .base import (
    CredentialsMissingError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
    TransientProviderError,
)

#: Credits per call, per Tavily's published pricing.
SEARCH_CREDITS = {"basic": 1, "advanced": 2}
EXTRACT_CREDITS = 1


@dataclass(slots=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    score: float = 0.0


@dataclass(slots=True)
class SearchResponse:
    query: str
    hits: list[SearchHit] = field(default_factory=list)
    credits: int = 0


@dataclass(slots=True)
class PageContent:
    url: str
    title: str
    text: str
    fetched_at: datetime
    credits: int = 0


@runtime_checkable
class SearchProvider(Protocol):
    name: str

    async def search(self, query: str, *, max_results: int = 5) -> SearchResponse: ...

    async def extract(self, url: str) -> PageContent: ...


def _map_error(exc: httpx.HTTPStatusError) -> ProviderError:
    status = exc.response.status_code
    detail = exc.response.text[:300]
    if status == 429:
        return RateLimitError(f"tavily rate limited: {detail}")
    if status in (401, 403):
        return CredentialsMissingError(f"tavily auth failed ({status}): {detail}")
    if status >= 500:
        return TransientProviderError(f"tavily upstream error {status}: {detail}")
    return ProviderError(f"tavily rejected request ({status}): {detail}", status_code=status)


class TavilySearchProvider(SearchProvider):
    name = "tavily"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.tavily.com",
        client: httpx.AsyncClient | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        if not api_key:
            raise CredentialsMissingError("TAVILY_API_KEY is required for live search")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient()
        self._timeout = timeout_s

    async def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        depth = "basic"
        payload = {
            "query": query,
            "max_results": max(1, min(max_results, 10)),
            "search_depth": depth,
            "include_raw_content": False,
            "include_answer": False,
        }
        body = await self._post("/search", payload)
        hits = [
            SearchHit(
                title=item.get("title") or item.get("url", ""),
                url=item.get("url", ""),
                snippet=(item.get("content") or "")[:600],
                score=float(item.get("score") or 0.0),
            )
            for item in body.get("results", [])
            if item.get("url")
        ]
        return SearchResponse(query=query, hits=hits, credits=SEARCH_CREDITS[depth])

    async def extract(self, url: str) -> PageContent:
        body = await self._post("/extract", {"urls": [url], "format": "text"})
        results = body.get("results") or []
        if not results:
            failed = body.get("failed_results") or []
            reason = failed[0].get("error") if failed else "no content returned"
            raise ProviderError(f"could not extract {url}: {reason}")

        item = results[0]
        return PageContent(
            url=item.get("url") or url,
            title=(item.get("title") or url)[:300],
            text=item.get("raw_content") or item.get("content") or "",
            fetched_at=datetime.now(UTC),
            credits=EXTRACT_CREDITS,
        )

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(
                f"{self._base_url}{path}",
                json=payload,
                headers={"Authorization": f"Bearer {self._api_key}"},
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise _map_error(exc) from exc
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"tavily timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"tavily transport error: {exc}") from exc
        return response.json()


class FixtureSearchProvider(SearchProvider):
    """Replays recorded Tavily responses so the flow runs with no key and no network.
    """

    name = "fixture_search"

    def __init__(self, fixture_dir: Path) -> None:
        self._dir = Path(fixture_dir)
        self._searches: dict[str, Any] = _load_json(self._dir / "searches.json", default={})
        self._pages: dict[str, Any] = _load_json(self._dir / "pages.json", default={})

    async def search(self, query: str, *, max_results: int = 5) -> SearchResponse:
        record = self._searches.get(query) or self._searches.get("__default__") or {}
        hits = [
            SearchHit(
                title=item["title"],
                url=item["url"],
                snippet=item.get("snippet", ""),
                score=float(item.get("score", 0.5)),
            )
            for item in record.get("results", [])[:max_results]
        ]
        return SearchResponse(query=query, hits=hits, credits=SEARCH_CREDITS["basic"])

    async def extract(self, url: str) -> PageContent:
        record = self._pages.get(url)
        if record is None:
            raise ProviderError(f"no fixture page recorded for {url}")
        return PageContent(
            url=url,
            title=f"[FIXTURE] {record['title']}",
            text=record["text"],
            fetched_at=datetime.now(UTC),
            credits=EXTRACT_CREDITS,
        )


def _load_json(path: Path, *, default: Any) -> Any:
    if not path.is_file():
        return default
    return json.loads(path.read_text(encoding="utf-8"))
