"""Assembles the provider bundle for the process.
"""

from __future__ import annotations
import re
from dataclasses import dataclass, field
from pathlib import Path
import httpx
from ..config import Settings
from ..domain.enums import ProviderMode, SizeStrategy
from ..logging import get_logger
from .azure_image import AzureImageProvider
from .azure_llm import AzureChatProvider
from .base import (
    ContentPolicyError,
    ImageProvider,
    LLMProvider,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
    TransientProviderError,
)
from .capabilities import ImageModelCapabilities, resolve_capabilities
from .fixtures import FixtureImageProvider, FixtureLLMProvider
from .limits import ConcurrencyGate, TokenBucketLimiter

log = get_logger(__name__)

FIXTURE_DIR = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures"

_INJECT_SPEC = re.compile(r"^(?P<kind>[a-z_0-9]+)(?:\*(?P<count>\d+))?$")

_INJECTABLE: dict[str, type[ProviderError]] = {
    "429": RateLimitError,
    "rate_limit": RateLimitError,
    "timeout": ProviderTimeoutError,
    "500": TransientProviderError,
    "transient": TransientProviderError,
    "content_policy": ContentPolicyError,
}


class FailureInjector:
    """Reproducible provider failures for review and tests.
    """

    def __init__(self, spec: dict[str, str]) -> None:
        self._budget: dict[str, tuple[type[ProviderError], int | None]] = {}
        for stage, raw in spec.items():
            match = _INJECT_SPEC.match(raw.strip().casefold())
            if not match:
                log.warning("failure_inject.unparseable", stage=stage, spec=raw)
                continue
            exc_type = _INJECTABLE.get(match.group("kind"))
            if exc_type is None:
                log.warning("failure_inject.unknown_kind", stage=stage, kind=match.group("kind"))
                continue
            count = match.group("count")
            self._budget[stage] = (exc_type, int(count) if count else None)
        self._fired: dict[str, int] = {}

    @property
    def active(self) -> bool:
        return bool(self._budget)

    def stages(self) -> list[str]:
        return sorted(self._budget)

    def maybe_fail(self, stage: str) -> None:
        entry = self._budget.get(stage)
        if entry is None:
            return
        exc_type, limit = entry
        fired = self._fired.get(stage, 0)
        if limit is not None and fired >= limit:
            return
        self._fired[stage] = fired + 1
        log.warning(
            "failure_inject.firing",
            stage=stage,
            kind=exc_type.kind,
            occurrence=fired + 1,
            limit=limit,
        )
        raise exc_type(f"injected {exc_type.kind} failure for stage {stage!r}")

    def reset(self) -> None:
        self._fired.clear()


@dataclass
class ProviderBundle:
    llm: LLMProvider
    image: ImageProvider
    capabilities: ImageModelCapabilities
    size_strategy: SizeStrategy
    image_gate: ConcurrencyGate
    injector: FailureInjector
    mode: ProviderMode
    image_model: str
    notes: list[str] = field(default_factory=list)
    _http: httpx.AsyncClient | None = None

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()


def build_providers(settings: Settings) -> ProviderBundle:
    """Construct providers and resolve the size strategy for this process."""
    injector = FailureInjector(settings.failure_injections())
    notes: list[str] = []
    image_model = settings.azure_openai_image_model or settings.azure_openai_image_deployment
    capabilities = resolve_capabilities(image_model)

    if capabilities.family == "unknown":
        notes.append(
            f"Image model {image_model!r} is not in the capability registry; "
            "assuming the conservative legacy profile (fixed 1024 size set)."
        )

    http: httpx.AsyncClient | None = None
    if settings.provider_mode is ProviderMode.LIVE:
        if missing := settings.missing_live_credentials():
            raise ProviderError(
                "PROVIDER_MODE=live but these are unset: " + ", ".join(missing)
            )
        http = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            follow_redirects=False,
        )
        llm: LLMProvider = AzureChatProvider(settings, http)
        image: ImageProvider = AzureImageProvider(settings, http)
        capabilities = image.capabilities
    else:
        notes.append(
            "FIXTURE MODE — research and imagery are replayed from recorded "
            "fixtures. No live browsing and no model calls were performed."
        )
        llm = FixtureLLMProvider()
        image = FixtureImageProvider()
        capabilities = image.capabilities

    strategy = capabilities.size_strategy
    if strategy is SizeStrategy.BANDS:
        notes.append(
            f"{capabilities.family} does not accept custom sizes, so the 9:16 export "
            "is reached by extending a 2:3 generation with palette bands rather "
            "than by generating an exact 9:16 frame."
        )

    gate = ConcurrencyGate(
        limiter=TokenBucketLimiter(rate_per_minute=settings.image_rpm, name="azure_images"),
        max_concurrent=2,
    )

    log.info(
        "providers.ready",
        mode=settings.provider_mode,
        image_model=image_model,
        family=capabilities.family,
        size_strategy=strategy,
        image_rpm=settings.image_rpm,
        failure_injection=injector.stages() or None,
    )

    return ProviderBundle(
        llm=llm,
        image=image,
        capabilities=capabilities,
        size_strategy=strategy,
        image_gate=gate,
        injector=injector,
        mode=settings.provider_mode,
        image_model=image_model,
        notes=notes,
        _http=http,
    )
