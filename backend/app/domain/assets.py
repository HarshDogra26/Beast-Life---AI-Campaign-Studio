"""Asset contracts and the size arithmetic that reaches the 1080-px export targets.
"""

from __future__ import annotations
from datetime import UTC, datetime
from fractions import Fraction
from math import gcd
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .enums import AssetFormat, SizeStrategy

# --- Azure image-model size rules ------------------------------------------
EDGE_MULTIPLE = 16
MIN_PIXELS = 655_360
MAX_PIXELS = 8_294_400
MAX_EDGE = 3_840
MIN_ASPECT = Fraction(1, 3)
MAX_ASPECT = Fraction(3, 1)

LEGACY_SIZES: frozenset[tuple[int, int]] = frozenset(
    {(1024, 1024), (1024, 1536), (1536, 1024)}
)

class Size(BaseModel):
    """An immutable pixel size with the vocabulary to reason about aspect."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    width: int = Field(gt=0, le=16_384)
    height: int = Field(gt=0, le=16_384)

    def __str__(self) -> str:
        return f"{self.width}x{self.height}"

    @property
    def pixels(self) -> int:
        return self.width * self.height

    @property
    def aspect(self) -> Fraction:
        return Fraction(self.width, self.height)

    @property
    def aspect_label(self) -> str:
        d = gcd(self.width, self.height)
        return f"{self.width // d}:{self.height // d}"


def validate_custom_size(size: Size) -> list[str]:
    """Return the reasons ``size`` violates Azure's custom-size rules (empty = OK)."""
    problems: list[str] = []
    for edge, name in ((size.width, "width"), (size.height, "height")):
        if edge % EDGE_MULTIPLE:
            problems.append(f"{name} {edge} is not a multiple of {EDGE_MULTIPLE}")
        if edge > MAX_EDGE:
            problems.append(f"{name} {edge} exceeds the {MAX_EDGE}px limit")
    if not MIN_PIXELS <= size.pixels <= MAX_PIXELS:
        problems.append(
            f"total pixels {size.pixels:,} outside [{MIN_PIXELS:,}, {MAX_PIXELS:,}]"
        )
    if not MIN_ASPECT <= size.aspect <= MAX_ASPECT:
        problems.append(f"aspect {size.aspect_label} outside 1:3..3:1")
    return problems


# --- Export targets named by the assignment --------------------------------
EXPORT_SIZES: dict[AssetFormat, Size] = {
    AssetFormat.SQUARE: Size(width=1080, height=1080),
    AssetFormat.VERTICAL: Size(width=1080, height=1920),
    AssetFormat.VIDEO: Size(width=1080, height=1920),
    # Same canvas as the vertical ad; it is that ad without the copy layer.
    AssetFormat.VERTICAL_PLATE: Size(width=1080, height=1920),
}

MASTER_SCENE_SIZE = Size(width=1024, height=1024)

NATIVE_GENERATION_SIZES: dict[AssetFormat, Size] = {
    AssetFormat.SQUARE: Size(width=1088, height=1088),
    AssetFormat.VERTICAL: Size(width=1152, height=2048),
}

BANDS_GENERATION_SIZES: dict[AssetFormat, Size] = {
    AssetFormat.SQUARE: Size(width=1024, height=1024),
    AssetFormat.VERTICAL: Size(width=1024, height=1536),
}


def generation_size(fmt: AssetFormat, strategy: SizeStrategy) -> Size:
    """The size we ask the model for, given the resolved strategy."""
    table = (
        NATIVE_GENERATION_SIZES
        if strategy is SizeStrategy.NATIVE
        else BANDS_GENERATION_SIZES
    )
    try:
        return table[fmt]
    except KeyError:
        raise ValueError(f"{fmt} is not an image ad format") from None


def is_exact_aspect(fmt: AssetFormat, strategy: SizeStrategy) -> bool:
    """Whether generation matches the export aspect, i.e. a pure downscale works.
    """
    return generation_size(fmt, strategy).aspect == EXPORT_SIZES[fmt].aspect


class GenerationRequest(BaseModel):
    """Exactly what was sent to the provider — persisted with every asset.
    """

    model_config = ConfigDict(extra="forbid")
    format: AssetFormat
    operation: str = Field(pattern=r"^(generations|edits|composite|render)$")
    model: str = Field(max_length=120)
    prompt: str = Field(max_length=8000)
    generated_size: Size
    export_size: Size
    quality: str | None = Field(default=None, max_length=20)
    input_fidelity: str | None = Field(default=None, max_length=20)
    strategy: SizeStrategy
    source_asset_id: str | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def _generated_size_is_legal(self) -> GenerationRequest:
        """A request that cannot succeed should never reach the network."""
        if self.operation in {"generations", "edits"}:
            if self.strategy is SizeStrategy.NATIVE:
                if problems := validate_custom_size(self.generated_size):
                    raise ValueError(
                        f"{self.generated_size} is not a legal custom size: "
                        + "; ".join(problems)
                    )
            elif (self.generated_size.width, self.generated_size.height) not in LEGACY_SIZES:
                raise ValueError(
                    f"{self.generated_size} is not accepted by the legacy size set "
                    f"{sorted(LEGACY_SIZES)}"
                )
        return self


class RenderedAsset(BaseModel):
    """A file on disk, with provenance."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=8, max_length=64)
    campaign_id: str = Field(min_length=8, max_length=64)
    spec_id: str | None = Field(default=None, max_length=64)
    format: AssetFormat
    artifact_path: str = Field(max_length=500)
    media_type: str = Field(max_length=100)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    byte_size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    duration_s: float | None = Field(default=None, gt=0)
    request: GenerationRequest
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _matches_export_target(self) -> RenderedAsset:
        """Assert the export contract at construction time.
        """
        if self.format is AssetFormat.MASTER_SCENE:
            return self
        expected = EXPORT_SIZES[self.format]
        if self.format is AssetFormat.VERTICAL_PLATE:
            if (self.width, self.height) != (expected.width, expected.height):
                raise ValueError(f"{self.format} must be exactly {expected}")
            return self
        if (self.width, self.height) != (expected.width, expected.height):
            raise ValueError(
                f"{self.format} must be exactly {expected}, got "
                f"{self.width}x{self.height}"
            )
        if self.format is AssetFormat.VIDEO:
            if self.duration_s is None:
                raise ValueError("video assets require a measured duration")
            if not 6.0 <= self.duration_s <= 10.0:
                raise ValueError(
                    f"video duration {self.duration_s:.2f}s outside the required 6-10s"
                )
        return self
