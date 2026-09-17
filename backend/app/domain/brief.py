"""The product brief — the single human-authored input to the whole workflow.
"""

from __future__ import annotations
import re
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from .enums import CampaignObjective

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]")

TrimmedStr = Annotated[str, Field(strip_whitespace=True)]


def _strip_control(value: str) -> str:
    return _CONTROL_CHARS.sub("", value).strip()


class VerifiedClaim(BaseModel):
    """A fact the user vouches for, with provenance.
    """

    model_config = ConfigDict(extra="forbid")
    text: TrimmedStr = Field(min_length=3, max_length=300)
    evidence: TrimmedStr | None = Field(
        default=None,
        max_length=500,
        description="Where this was verified — a URL, lab reference, or internal note.",
    )

    @field_validator("text", "evidence", mode="before")
    @classmethod
    def _clean(cls, v: object) -> object:
        return _strip_control(v) if isinstance(v, str) else v


class ProductBrief(BaseModel):
    """Server-validated campaign input.
    """

    model_config = ConfigDict(extra="forbid")
    product_name: TrimmedStr = Field(min_length=2, max_length=120)
    product_description: TrimmedStr = Field(
        min_length=20,
        max_length=2000,
        description="Factual description. The only source of product truth.",
    )
    target_audience: TrimmedStr = Field(min_length=5, max_length=500)
    campaign_objective: CampaignObjective
    tone: TrimmedStr = Field(min_length=3, max_length=200)
    call_to_action: TrimmedStr = Field(min_length=2, max_length=60)
    verified_claims: list[VerifiedClaim] = Field(default_factory=list, max_length=20)
    reference_image_id: str | None = Field(
        default=None,
        max_length=64,
        description="Artifact id of an uploaded packshot, if any.",
    )

    @field_validator(
        "product_name",
        "product_description",
        "target_audience",
        "tone",
        "call_to_action",
        mode="before",
    )
    @classmethod
    def _clean(cls, v: object) -> object:
        return _strip_control(v) if isinstance(v, str) else v

    @field_validator("reference_image_id")
    @classmethod
    def _safe_artifact_id(cls, v: str | None) -> str | None:
        """Artifact ids are interpolated into filesystem paths — keep them opaque."""
        if v is not None and not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", v):
            raise ValueError("reference_image_id must be 8-64 chars of [A-Za-z0-9_-]")
        return v

    @model_validator(mode="after")
    def _cta_is_not_a_sentence(self) -> ProductBrief:
        """A CTA is a button label. Long CTAs break the layout engine's fitting."""
        if len(self.call_to_action.split()) > 8:
            raise ValueError("call_to_action must be 8 words or fewer")
        return self

    def claim_corpus(self) -> str:
        """Everything the generator is permitted to treat as product truth.
        """
        parts = [self.product_description, *(c.text for c in self.verified_claims)]
        return "\n".join(parts)
