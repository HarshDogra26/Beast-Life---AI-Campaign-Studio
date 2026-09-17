"""The shared creative specification — the single source of truth for all assets.
"""

from __future__ import annotations
import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Self
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

HEX_COLOR = r"^#[0-9A-Fa-f]{6}$"

_FORBIDDEN_CLAIM_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("numeric_performance", re.compile(r"\b\d+(?:\.\d+)?\s*%|\b\d+x\s+(?:more|faster|stronger|better)\b", re.I)),
    ("certification", re.compile(r"\b(?:clinically|scientifically|lab)\s+(?:proven|tested|validated)|\bFDA\b|\bcertified\b|\bapproved by\b", re.I)),
    ("discount", re.compile(r"\b(?:\d+%\s*off|sale|discount|free shipping|limited time|save \$?\d+)\b", re.I)),
    ("superlative", re.compile(r"\b(?:best|#1|number one|world'?s leading|guaranteed|fastest|strongest)\b", re.I)),
    ("health_claim", re.compile(r"\b(?:cures?|treats?|prevents?|heals?)\b", re.I)),
)


class ClaimViolation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: str
    category: str
    matched_text: str
    message: str


def audit_copy(text: str, *, field: str) -> list[ClaimViolation]:
    """Flag categories of claim that no brief can authorise us to invent."""
    violations: list[ClaimViolation] = []
    for category, pattern in _FORBIDDEN_CLAIM_PATTERNS:
        if match := pattern.search(text):
            violations.append(
                ClaimViolation(
                    field=field,
                    category=category,
                    matched_text=match.group(0),
                    message=(
                        f"{field} contains a {category.replace('_', ' ')} claim "
                        f"({match.group(0)!r}) which the brief does not support"
                    ),
                )
            )
    return violations


class ProductIdentity(BaseModel):
    """What the product *is* — held constant across every generated asset."""

    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=2, max_length=120)
    form_factor: str = Field(
        min_length=3,
        max_length=200,
        description="Physical form, e.g. 'matte black 1kg tub with screw lid'.",
    )
    packaging_description: str = Field(min_length=10, max_length=600)
    reference_image_id: str | None = Field(default=None, max_length=64)


class SceneSpec(BaseModel):
    """The one scene, described once, re-framed per format."""

    model_config = ConfigDict(extra="forbid")
    setting: str = Field(min_length=5, max_length=400)
    subject: str = Field(min_length=5, max_length=400)
    lighting: str = Field(min_length=3, max_length=200)
    mood: str = Field(min_length=3, max_length=200)
    props: list[str] = Field(default_factory=list, max_length=10)


class Palette(BaseModel):
    """Shared colour system. Drives both image overlays and video frames."""

    model_config = ConfigDict(extra="forbid")
    primary: str = Field(pattern=HEX_COLOR)
    secondary: str = Field(pattern=HEX_COLOR)
    accent: str = Field(pattern=HEX_COLOR)
    background: str = Field(pattern=HEX_COLOR)

    def as_tuple(self, name: str) -> tuple[int, int, int]:
        value: str = getattr(self, name)
        return tuple(int(value[i : i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


class CompositionGuidance(BaseModel):
    """Per-format framing. This is what makes the two ads *adapted*, not stretched."""

    model_config = ConfigDict(extra="forbid")
    framing: str = Field(min_length=10, max_length=400)
    product_placement: str = Field(min_length=5, max_length=300)
    text_safe_zone: str = Field(
        min_length=5,
        max_length=300,
        description="Where the deterministic overlay will sit; the image must leave it calm.",
    )


class VideoBeat(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=2, max_length=60)
    seconds: float = Field(gt=0.4, le=6.0)
    on_screen_text: str = Field(max_length=120)
    motion: str = Field(min_length=3, max_length=200)


class VideoOutline(BaseModel):
    """Storyboard for the 6–10s vertical cut."""

    model_config = ConfigDict(extra="forbid")
    beats: list[VideoBeat] = Field(min_length=2, max_length=5)
    transition: str = Field(default="crossfade", max_length=40)

    @property
    def total_seconds(self) -> float:
        return round(sum(b.seconds for b in self.beats), 3)

    @model_validator(mode="after")
    def _duration_in_range(self) -> Self:
        """Enforce the assignment's 6–10s window at spec time, not render time.

        Catching it here means an out-of-range storyboard never reaches ffmpeg.
        """
        if not 6.0 <= self.total_seconds <= 10.0:
            raise ValueError(
                f"video beats total {self.total_seconds}s; must be between 6 and 10s"
            )
        return self

    @model_validator(mode="after")
    def _ends_on_cta(self) -> Self:
        if not self.beats[-1].on_screen_text.strip():
            raise ValueError("final beat must carry the CTA as on-screen text")
        return self


class CampaignSpec(BaseModel):
    """Versioned, validated, and shared by every asset stage."""

    model_config = ConfigDict(extra="forbid")
    spec_id: str = Field(min_length=8, max_length=64)
    version: int = Field(default=1, ge=1)
    campaign_id: str = Field(min_length=8, max_length=64)
    selected_angle_id: str = Field(pattern=r"^a[1-9]\d?$")
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    hook: str = Field(min_length=3, max_length=200)
    headline: str = Field(min_length=3, max_length=90, description="Approved copy.")
    subhead: str | None = Field(default=None, max_length=140)
    cta_text: str = Field(min_length=2, max_length=60)
    product: ProductIdentity
    scene: SceneSpec
    palette: Palette
    composition: dict[str, CompositionGuidance] = Field(min_length=2)
    video: VideoOutline
    claim_violations: list[ClaimViolation] = Field(default_factory=list)

    @field_validator("headline", "subhead", "cta_text", "hook")
    @classmethod
    def _single_line(cls, v: str | None) -> str | None:
        """Overlay text is laid out by us; embedded newlines break the fitter."""
        if v is None:
            return None
        cleaned = " ".join(v.split())
        if not cleaned:
            raise ValueError("must not be blank")
        return cleaned

    @model_validator(mode="after")
    def _composition_covers_both_formats(self) -> Self:
        required = {"square", "vertical"}
        if missing := required - set(self.composition):
            raise ValueError(f"composition guidance missing for: {sorted(missing)}")
        return self

    @model_validator(mode="after")
    def _no_unapproved_claims(self) -> Self:
        """Structural refusal to build assets from copy making forbidden claims."""
        found: list[ClaimViolation] = []
        for field in ("headline", "subhead", "hook", "cta_text"):
            if value := getattr(self, field):
                found.extend(audit_copy(value, field=field))
        for beat in self.video.beats:
            found.extend(audit_copy(beat.on_screen_text, field=f"video.{beat.label}"))

        if found:
            self.claim_violations = found
            raise ValueError(
                "copy contains unsupported claims: "
                + "; ".join(v.message for v in found)
            )
        return self

    def fingerprint(self) -> str:
        """Stable hash of the creative content, ignoring identity/timestamps.
        """
        payload = self.model_dump(
            mode="json",
            exclude={"spec_id", "created_at", "campaign_id", "claim_violations"},
        )
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()
