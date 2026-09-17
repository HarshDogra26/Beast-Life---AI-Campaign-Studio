"""JSON schemas for structured model output.
"""

from __future__ import annotations
from typing import Any


def _obj(properties: dict[str, Any], **extra: Any) -> dict[str, Any]:
    """Object schema with strict-mode defaults applied."""
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
        **extra,
    }


_SOURCE_ID_ARRAY = {
    "type": "array",
    "items": {"type": "string", "pattern": "^s\\d{1,3}$"},
    "minItems": 1,
    "maxItems": 10,
}

ANGLE_PROPOSALS_SCHEMA: dict[str, Any] = {
    "title": "AngleProposals",
    **_obj(
        {
            "angles": {
                "type": "array",
                "minItems": 3,
                "maxItems": 3,
                "items": _obj(
                    {
                        "id": {
                            "type": "string",
                            "pattern": "^a[1-9]\\d?$",
                            "description": "a1, a2, a3 in order.",
                        },
                        "title": {"type": "string", "minLength": 3, "maxLength": 120},
                        "audience_insight": {
                            "type": "string",
                            "minLength": 20,
                            "maxLength": 800,
                            "description": (
                                "SOURCED. Something a cited page actually says about the "
                                "audience. Not your opinion."
                            ),
                        },
                        "insight_source_ids": {
                            **_SOURCE_ID_ARRAY,
                            "description": "Ids of pages that support audience_insight.",
                        },
                        "hook": {"type": "string", "minLength": 3, "maxLength": 200},
                        "visual_direction": {
                            "type": "string",
                            "minLength": 20,
                            "maxLength": 800,
                            "description": (
                                "INTERPRETED. A photographable scene: setting, subject, "
                                "lighting, mood, composition. No slogans, no claims."
                            ),
                        },
                        "rationale": {
                            "type": "string",
                            "minLength": 20,
                            "maxLength": 800,
                            "description": (
                                "INTERPRETED. Creative judgement only. Must not assert "
                                "performance or popularity facts."
                            ),
                        },
                        "supporting_source_ids": _SOURCE_ID_ARRAY,
                    }
                ),
            }
        }
    ),
}


_COMPOSITION = _obj(
    {
        "framing": {"type": "string", "minLength": 10, "maxLength": 400},
        "product_placement": {"type": "string", "minLength": 5, "maxLength": 300},
        "text_safe_zone": {
            "type": "string",
            "minLength": 5,
            "maxLength": 300,
            "description": "Where the image stays quiet so overlay text stays legible.",
        },
    }
)

_HEX = {"type": "string", "pattern": "^#[0-9A-Fa-f]{6}$"}

CAMPAIGN_SPEC_SCHEMA: dict[str, Any] = {
    "title": "CampaignSpecDraft",
    **_obj(
        {
            "headline": {
                "type": "string",
                "minLength": 3,
                "maxLength": 90,
                "description": "Approved copy. One line. No unsupported claims.",
            },
            "subhead": {
                "type": ["string", "null"],
                "maxLength": 140,
                "description": "Optional supporting line, or null.",
            },
            "hook": {"type": "string", "minLength": 3, "maxLength": 200},
            "product": _obj(
                {
                    "form_factor": {"type": "string", "minLength": 3, "maxLength": 200},
                    "packaging_description": {
                        "type": "string",
                        "minLength": 10,
                        "maxLength": 600,
                    },
                }
            ),
            "scene": _obj(
                {
                    "setting": {"type": "string", "minLength": 5, "maxLength": 400},
                    "subject": {"type": "string", "minLength": 5, "maxLength": 400},
                    "lighting": {"type": "string", "minLength": 3, "maxLength": 200},
                    "mood": {"type": "string", "minLength": 3, "maxLength": 200},
                    "props": {
                        "type": "array",
                        "items": {"type": "string", "maxLength": 80},
                        "maxItems": 10,
                    },
                }
            ),
            "palette": _obj(
                {
                    "primary": _HEX,
                    "secondary": _HEX,
                    "accent": _HEX,
                    "background": _HEX,
                }
            ),
            "composition": _obj({"square": _COMPOSITION, "vertical": _COMPOSITION}),
            "video": _obj(
                {
                    "beats": {
                        "type": "array",
                        "minItems": 2,
                        "maxItems": 5,
                        "items": _obj(
                            {
                                "label": {"type": "string", "minLength": 2, "maxLength": 60},
                                "seconds": {
                                    "type": "number",
                                    "minimum": 0.5,
                                    "maximum": 6.0,
                                },
                                "on_screen_text": {"type": "string", "maxLength": 120},
                                "motion": {
                                    "type": "string",
                                    "minLength": 3,
                                    "maxLength": 200,
                                },
                            }
                        ),
                    },
                    "transition": {"type": "string", "maxLength": 40},
                }
            ),
        }
    ),
}
