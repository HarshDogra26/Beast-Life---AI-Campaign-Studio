"""Declarative capability registry for Azure image deployments.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from ..domain.enums import SizeStrategy


@dataclass(frozen=True, slots=True)
class ImageModelCapabilities:
    family: str
    supports_custom_sizes: bool
    supports_edits: bool
    supports_input_fidelity: bool
    qualities: frozenset[str]
    default_quality: str
    notes: str = ""
    aliases: tuple[str, ...] = field(default_factory=tuple)

    @property
    def size_strategy(self) -> SizeStrategy:
        return SizeStrategy.NATIVE if self.supports_custom_sizes else SizeStrategy.BANDS

    def coerce_quality(self, requested: str) -> str:
        """Fall back to the model's default rather than sending an invalid value."""
        return requested if requested in self.qualities else self.default_quality


_CUSTOM_SIZE_QUALITIES = frozenset({"low", "medium", "high", "xhigh", "max", "auto"})
_LEGACY_QUALITIES = frozenset({"low", "medium", "high"})

_REGISTRY: tuple[ImageModelCapabilities, ...] = (
    ImageModelCapabilities(
        family="gpt-image-2.5-sunburst",
        supports_custom_sizes=True,
        supports_edits=True,
        supports_input_fidelity=False,
        qualities=_CUSTOM_SIZE_QUALITIES,
        default_quality="high",
        notes="GA. Highest fidelity; documented as best when editing precision matters.",
        aliases=("sunburst", "gpt-image-2.5"),
    ),
    ImageModelCapabilities(
        family="gpt-image-2.5-flare",
        supports_custom_sizes=True,
        supports_edits=True,
        supports_input_fidelity=False,
        qualities=_CUSTOM_SIZE_QUALITIES,
        default_quality="high",
        notes="GA. Fastest at identical token pricing to sunburst.",
        aliases=("flare",),
    ),
    ImageModelCapabilities(
        family="gpt-image-2",
        supports_custom_sizes=True,
        supports_edits=True,
        supports_input_fidelity=True,
        qualities=_LEGACY_QUALITIES,
        default_quality="high",
        notes="GA. Arbitrary resolutions up to 4K, face preservation.",
    ),
    ImageModelCapabilities(
        family="gpt-image-1.5",
        supports_custom_sizes=False,
        supports_edits=True,
        supports_input_fidelity=True,
        qualities=_LEGACY_QUALITIES,
        default_quality="high",
        notes="Limited-access preview. Locked to the 1024 size set.",
    ),
    ImageModelCapabilities(
        family="gpt-image-1-mini",
        supports_custom_sizes=False,
        supports_edits=True,
        supports_input_fidelity=False,
        qualities=_LEGACY_QUALITIES,
        default_quality="medium",
        notes="Limited-access preview. No input_fidelity, no face preservation.",
    ),
    ImageModelCapabilities(
        family="gpt-image-1",
        supports_custom_sizes=False,
        supports_edits=True,
        supports_input_fidelity=True,
        qualities=_LEGACY_QUALITIES,
        default_quality="high",
        notes="Limited-access preview. Locked to the 1024 size set.",
    ),
)

#: Conservative profile for an unrecognised deployment.
UNKNOWN_MODEL = ImageModelCapabilities(
    family="unknown",
    supports_custom_sizes=False,
    supports_edits=True,
    supports_input_fidelity=False,
    qualities=_LEGACY_QUALITIES,
    default_quality="medium",
    notes="Unrecognised model; assuming the conservative legacy profile.",
)


def resolve_capabilities(model_name: str) -> ImageModelCapabilities:
    """Best-effort family lookup for a configured model or deployment name.
    """
    needle = (model_name or "").strip().casefold()
    if not needle:
        return UNKNOWN_MODEL

    for caps in sorted(_REGISTRY, key=lambda c: len(c.family), reverse=True):
        if needle == caps.family:
            return caps

    for caps in sorted(_REGISTRY, key=lambda c: len(c.family), reverse=True):
        candidates = (caps.family, *caps.aliases)
        if any(c in needle for c in candidates):
            return caps

    return UNKNOWN_MODEL


def known_families() -> list[str]:
    return [c.family for c in _REGISTRY]
