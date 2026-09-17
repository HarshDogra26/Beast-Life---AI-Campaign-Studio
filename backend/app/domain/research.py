"""Research contracts: sources, the agent's tool trace, and creative angles.
"""

from __future__ import annotations
import re
from datetime import datetime
from typing import Annotated, Self
from urllib.parse import urlparse
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from .enums import ToolName

SourceId = Annotated[str, Field(pattern=r"^s\d{1,3}$")]

_EVIDENCE_WORDS = re.compile(
    r"\b("
    r"trending|trends?|viral|high[- ]converting|best[- ]performing|proven|"
    r"outperform\w*|top[- ]rated|#1|number one|most popular|fastest[- ]growing|"
    r"studies show|research shows|statistics? show"
    r")\b",
    re.IGNORECASE,
)

_INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("instruction_override", re.compile(r"ignore\s+(?:all\s+)?(?:previous|prior|above)\s+instructions?", re.I)),
    ("role_hijack", re.compile(r"\b(?:you are now|new instructions?|system\s*(?:prompt|message)\s*:)", re.I)),
    ("exfiltration", re.compile(r"\b(?:reveal|print|output|disclose)\s+(?:your\s+)?(?:system\s+prompt|api[_ ]?key|secret)", re.I)),
    ("fence_escape", re.compile(r"</?untrusted_source", re.I)),
    ("tool_coercion", re.compile(r"\b(?:call|invoke|use)\s+the\s+\w+\s+tool\b", re.I)),
)


def detect_injection(text: str) -> list[str]:
    """Return the names of injection heuristics that fired on ``text``."""
    return [name for name, pattern in _INJECTION_PATTERNS if pattern.search(text)]


class SourceDoc(BaseModel):
    """One web page the agent actually read.
    """

    model_config = ConfigDict(extra="forbid")

    id: SourceId
    url: HttpUrl
    title: str = Field(min_length=1, max_length=300)
    accessed_at: datetime
    excerpt: str = Field(
        min_length=1,
        max_length=1200,
        description="Verbatim supporting passage from the page.",
    )
    summary: str = Field(min_length=1, max_length=800)
    injection_flags: list[str] = Field(default_factory=list)

    @property
    def domain(self) -> str:
        return urlparse(str(self.url)).netloc

    @field_validator("title", "excerpt", "summary")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must not be blank")
        return v.strip()


class ToolCallRecord(BaseModel):
    """One observable tool invocation. This is the 'show its tool calls' evidence."""

    model_config = ConfigDict(extra="forbid")
    tool: ToolName
    arguments: dict[str, object]
    started_at: datetime
    latency_ms: int = Field(ge=0)
    ok: bool
    result_summary: str = Field(max_length=600)
    error: str | None = Field(default=None, max_length=600)
    credits_spent: int = Field(default=0, ge=0)


class AgentStep(BaseModel):
    """One turn of the research loop: what it decided, and what it did about it."""

    model_config = ConfigDict(extra="forbid")
    index: int = Field(ge=0)
    decision_summary: str = Field(min_length=1, max_length=600)
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


class ResearchBudget(BaseModel):
    """Configured ceilings, echoed into the report so limits are visible in the UI."""

    model_config = ConfigDict(extra="forbid")
    max_search_calls: int = Field(ge=1, le=20)
    max_fetch_calls: int = Field(ge=1, le=40)
    wall_clock_s: float = Field(gt=0, le=900)


class BudgetUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    search_calls: int = Field(default=0, ge=0)
    fetch_calls: int = Field(default=0, ge=0)
    elapsed_s: float = Field(default=0.0, ge=0)
    exhausted_reason: str | None = None


class CreativeAngle(BaseModel):
    """One recommended creative direction.
    """

    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^a[1-9]\d?$")
    title: str = Field(min_length=3, max_length=120)

    audience_insight: str = Field(
        min_length=20,
        max_length=800,
        description="SOURCED observation about the audience. Must cite.",
    )
    insight_source_ids: list[SourceId] = Field(min_length=1, max_length=10)
    hook: str = Field(min_length=3, max_length=200)
    visual_direction: str = Field(min_length=20, max_length=800)
    rationale: str = Field(
        min_length=20,
        max_length=800,
        description="INTERPRETED. Our creative reasoning, not a sourced fact.",
    )
    supporting_source_ids: list[SourceId] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def _interpretation_must_not_pose_as_evidence(self) -> Self:
        """Creative fields may not make empirical performance claims.
        """
        for field in ("hook", "visual_direction", "rationale"):
            if match := _EVIDENCE_WORDS.search(getattr(self, field)):
                raise ValueError(
                    f"{field} makes an evidence claim ({match.group(0)!r}) but is an "
                    "interpretation field; move it to audience_insight with a citation"
                )
        return self

    def cited_ids(self) -> set[str]:
        return {*self.insight_source_ids, *self.supporting_source_ids}


class ResearchReport(BaseModel):
    """The complete, user-inspectable output of the research stage."""

    model_config = ConfigDict(extra="forbid")
    sources: list[SourceDoc] = Field(default_factory=list)
    steps: list[AgentStep] = Field(default_factory=list)
    angles: list[CreativeAngle] = Field(min_length=3, max_length=3)
    budget: ResearchBudget
    usage: BudgetUsage
    coverage_gap: str | None = Field(default=None, max_length=600)

    @model_validator(mode="after")
    def _citations_resolve(self) -> Self:
        """Every cited source id must be a page we actually fetched.
        """
        known = {s.id for s in self.sources}
        for angle in self.angles:
            if unknown := sorted(angle.cited_ids() - known):
                raise ValueError(
                    f"angle {angle.id} cites unknown source ids {unknown}; "
                    f"known ids are {sorted(known)}"
                )
        return self

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        for label, ids in (
            ("source", [s.id for s in self.sources]),
            ("angle", [a.id for a in self.angles]),
        ):
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate {label} ids: {ids}")
        return self

    @model_validator(mode="after")
    def _distinct_angles(self) -> Self:
        """'Return 3 distinct creative angles' — enforce it, don't hope for it."""
        hooks = [a.hook.strip().casefold() for a in self.angles]
        if len(set(hooks)) != len(hooks):
            raise ValueError("angles must be distinct; duplicate hooks found")
        return self

    @model_validator(mode="after")
    def _gap_reported_when_thin(self) -> Self:
        """Under-sourced runs must say so explicitly."""
        if len(self.sources) < 3 and not self.coverage_gap:
            raise ValueError(
                f"only {len(self.sources)} source(s) gathered; coverage_gap must "
                "explain the shortfall"
            )
        return self

    @property
    def flagged_sources(self) -> list[SourceDoc]:
        return [s for s in self.sources if s.injection_flags]
