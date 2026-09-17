"""Provider interfaces and the error taxonomy.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class ProviderError(RuntimeError):
    """Base class. ``kind`` is persisted on the stage row for the UI."""

    kind: str = "provider_error"
    retryable: bool = False

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class RateLimitError(ProviderError):
    kind = "rate_limit"
    retryable = True

    def __init__(
        self, message: str, *, status_code: int | None = 429, retry_after_s: float | None = None
    ) -> None:
        super().__init__(message, status_code=status_code)
        self.retry_after_s = retry_after_s


class ProviderTimeoutError(ProviderError):
    kind = "timeout"
    retryable = True


class TransientProviderError(ProviderError):
    """5xx and connection faults."""

    kind = "transient"
    retryable = True


class ContentPolicyError(ProviderError):
    """Rejected by content safety. Terminal — the same prompt will be rejected again."""

    kind = "content_policy"
    retryable = False


class SizeRejectedError(ProviderError):
    """The deployment refused the requested size.
    """

    kind = "size_rejected"
    retryable = False


class UnsupportedParameterError(ProviderError):
    """The deployment rejected an optional request parameter.
    """

    kind = "unsupported_parameter"
    retryable = False

    def __init__(self, message: str, *, parameter: str, status_code: int | None = 400) -> None:
        super().__init__(message, status_code=status_code)
        self.parameter = parameter


class MalformedOutputError(ProviderError):
    """Model returned something that failed contract validation.
    """

    kind = "malformed_output"
    retryable = True


class CredentialsMissingError(ProviderError):
    kind = "credentials_missing"
    retryable = False


class NoSourcesError(ProviderError):
    """Research finished without reading a single page.

    Failed here rather than downstream because it is *structurally*
    unrecoverable: an angle must cite at least one source id that resolves to a
    fetched page, so with zero sources the synthesis contract cannot be
    satisfied by any output the model could produce. Letting it through would
    burn a repair loop on an impossible constraint and report the error against
    the wrong stage.
    """

    kind = "no_sources"
    retryable = False


class DeploymentNotFoundError(ProviderError):
    """No deployment by that name exists in the resource.
    """

    kind = "deployment_not_found"
    retryable = False


@dataclass(slots=True)
class LLMUsage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@dataclass(slots=True)
class LLMResult:
    text: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    model: str = ""


@dataclass(slots=True)
class ToolCall:
    """A tool invocation the model asked for."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ChatTurn:
    """One assistant turn: optional prose plus any tool calls it requested.
    """

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: LLMUsage = field(default_factory=LLMUsage)
    model: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


@dataclass(slots=True)
class ToolSchema:
    """An OpenAI-format function tool, converted from an MCP tool definition."""

    name: str
    description: str
    parameters: dict[str, Any]

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description[:1024],
                "parameters": self.parameters,
            },
        }


@dataclass(slots=True)
class ImageResult:
    """Raw image bytes plus whatever usage the provider reported.
    """

    data: bytes
    media_type: str = "image/png"
    model: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None


@runtime_checkable
class LLMProvider(Protocol):
    """Text generation, optionally constrained to a JSON schema."""

    name: str

    async def complete(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResult: ...

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[ToolSchema] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> ChatTurn:
        """Multi-turn chat with native tool calling. Drives the research loop."""
        ...


@runtime_checkable
class ImageProvider(Protocol):
    name: str
    model: str

    async def generate(
        self, *, prompt: str, size: str, quality: str
    ) -> ImageResult: ...

    async def edit(
        self,
        *,
        prompt: str,
        size: str,
        quality: str,
        images: list[tuple[str, bytes, str]],
        input_fidelity: str | None = None,
    ) -> ImageResult: ...
