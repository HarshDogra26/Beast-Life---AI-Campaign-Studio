"""Azure OpenAI chat adapter.
"""

from __future__ import annotations
import json
from typing import Any
import httpx
from ..config import Settings
from ..logging import get_logger
from .base import (
    ChatTurn,
    ContentPolicyError,
    CredentialsMissingError,
    LLMProvider,
    LLMResult,
    LLMUsage,
    MalformedOutputError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
    ToolCall,
    ToolSchema,
    TransientProviderError,
)

log = get_logger(__name__)


def parse_tool_calls(message: dict[str, Any]) -> list[ToolCall]:
    """Extract tool calls from an assistant message.
    """
    calls: list[ToolCall] = []
    for raw_call in message.get("tool_calls") or []:
        function = raw_call.get("function") or {}
        name = function.get("name")
        if not name:
            continue
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except json.JSONDecodeError:
            log.warning("agent.tool_arguments_unparseable", tool=name)
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        calls.append(ToolCall(id=raw_call.get("id") or name, name=name, arguments=arguments))
    return calls


def map_http_error(exc: httpx.HTTPStatusError) -> ProviderError:
    """Translate an Azure HTTP error into a typed, retry-aware provider error."""
    response = exc.response
    status = response.status_code
    try:
        body = response.json()
    except Exception:
        body = {}

    err = body.get("error", body) if isinstance(body, dict) else {}
    code = str(err.get("code", "")).lower() if isinstance(err, dict) else ""
    message = (
        err.get("message") if isinstance(err, dict) else None
    ) or response.text[:400] or f"HTTP {status}"

    if status == 429:
        retry_after = response.headers.get("retry-after")
        return RateLimitError(
            f"rate limited: {message}",
            retry_after_s=float(retry_after) if retry_after and retry_after.isdigit() else None,
        )
    if status in (408, 504):
        return ProviderTimeoutError(f"upstream timeout: {message}", status_code=status)
    if status >= 500:
        return TransientProviderError(f"upstream error {status}: {message}", status_code=status)
    if status == 401 or status == 403:
        return CredentialsMissingError(f"auth failed ({status}): {message}", status_code=status)
    if "content_filter" in code or "content_policy" in code or "responsible" in message.lower():
        return ContentPolicyError(f"content policy: {message}", status_code=status)
    return ProviderError(f"request rejected ({status}): {message}", status_code=status)


class AzureChatProvider(LLMProvider):
    name = "azure_openai_chat"

    def __init__(self, settings: Settings, client: httpx.AsyncClient) -> None:
        if not settings.azure_openai_endpoint or not settings.azure_openai_api_key.get_secret_value():
            raise CredentialsMissingError(
                "AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY are required in live mode"
            )
        self._settings = settings
        self._client = client
        self.model = settings.azure_openai_chat_deployment
        self._url = (
            f"{settings.azure_openai_endpoint.rstrip('/')}"
            f"/openai/deployments/{settings.azure_openai_chat_deployment}"
            f"/chat/completions?api-version={settings.azure_openai_chat_api_version}"
        )

    async def complete(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResult:
        payload: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_schema is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": json_schema.get("title", "response"),
                    "strict": True,
                    "schema": json_schema,
                },
            }

        try:
            response = await self._client.post(
                self._url,
                json=payload,
                headers={"api-key": self._settings.azure_openai_api_key.get_secret_value()},
                timeout=self._settings.llm_timeout_s,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise map_http_error(exc) from exc
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"chat request exceeded LLM_TIMEOUT_S="
                f"{self._settings.llm_timeout_s:.0f}s ({type(exc).__name__}). "
                "Raise it if prompts are large, or check Azure latency."
            ) from exc
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"chat transport error: {exc}") from exc

        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            raise MalformedOutputError("chat response contained no choices")

        choice = choices[0]
        if choice.get("finish_reason") == "content_filter":
            raise ContentPolicyError("response blocked by content filter")
        # A truncated JSON body will not parse downstream; say why up front.
        if choice.get("finish_reason") == "length" and json_schema is not None:
            raise MalformedOutputError(
                "response hit max_tokens before completing the JSON object"
            )

        text = (choice.get("message") or {}).get("content") or ""
        if not text.strip():
            raise MalformedOutputError("chat response was empty")

        usage_body = body.get("usage") or {}
        return LLMResult(
            text=text,
            usage=LLMUsage(
                prompt_tokens=usage_body.get("prompt_tokens"),
                completion_tokens=usage_body.get("completion_tokens"),
            ),
            model=body.get("model") or self.model,
        )

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[ToolSchema] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> ChatTurn:
        """Native tool-calling turn. This is what makes the research agent real:
        the model picks the tool and the arguments, we only enforce the budget."""
        payload: dict[str, Any] = {
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            payload["tools"] = [t.to_openai() for t in tools]
            payload["tool_choice"] = "auto"
            payload["parallel_tool_calls"] = False

        try:
            response = await self._client.post(
                self._url,
                json=payload,
                headers={"api-key": self._settings.azure_openai_api_key.get_secret_value()},
                timeout=self._settings.llm_timeout_s,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise map_http_error(exc) from exc
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"chat request exceeded LLM_TIMEOUT_S="
                f"{self._settings.llm_timeout_s:.0f}s ({type(exc).__name__}). "
                "Raise it if prompts are large, or check Azure latency."
            ) from exc
        except httpx.HTTPError as exc:
            raise TransientProviderError(f"chat transport error: {exc}") from exc

        body = response.json()
        choices = body.get("choices") or []
        if not choices:
            raise MalformedOutputError("chat response contained no choices")

        choice = choices[0]
        message = choice.get("message") or {}

        usage_body = body.get("usage") or {}
        return ChatTurn(
            content=message.get("content") or "",
            tool_calls=parse_tool_calls(message),
            finish_reason=choice.get("finish_reason") or "stop",
            usage=LLMUsage(
                prompt_tokens=usage_body.get("prompt_tokens"),
                completion_tokens=usage_body.get("completion_tokens"),
            ),
            model=body.get("model") or self.model,
        )


def parse_json_response(text: str) -> dict[str, Any]:
    """Parse a model's JSON reply, tolerating a markdown fence.
    """
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = candidate.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        candidate = "\n".join(lines).strip()

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise MalformedOutputError(
            f"model output was not valid JSON ({exc.msg} at position {exc.pos})"
        ) from exc

    if not isinstance(parsed, dict):
        raise MalformedOutputError(
            f"expected a JSON object, got {type(parsed).__name__}"
        )
    return parsed
