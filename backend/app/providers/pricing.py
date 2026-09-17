"""Cost estimation.
"""

from __future__ import annotations
import json
from functools import lru_cache
from pathlib import Path

_PRICING_PATH = Path(__file__).with_name("pricing.json")


@lru_cache(maxsize=1)
def _rates() -> dict:
    return json.loads(_PRICING_PATH.read_text(encoding="utf-8"))


def _family(model: str) -> str:
    needle = (model or "").casefold()
    table = _rates()["per_million_tokens"]
    for key in sorted(table, key=len, reverse=True):
        if key != "default_chat" and key in needle:
            return key
    return ""


def estimate_image_cost_usd(
    *, model: str, input_tokens: int | None, output_tokens: int | None
) -> float | None:
    """Estimated USD for one image call, or ``None`` if usage was not reported."""
    if input_tokens is None and output_tokens is None:
        return None
    family = _family(model)
    if not family:
        return None
    rate = _rates()["per_million_tokens"][family]
    cost = 0.0
    if input_tokens:
        cost += input_tokens * rate.get("image_input", 0.0) / 1_000_000
    if output_tokens:
        cost += output_tokens * rate.get("image_output", 0.0) / 1_000_000
    return round(cost, 6)


def estimate_chat_cost_usd(
    *, model: str, prompt_tokens: int | None, completion_tokens: int | None
) -> float | None:
    if prompt_tokens is None and completion_tokens is None:
        return None
    rates = _rates()
    needle = (model or "").casefold()
    rate = next(
        (v for k, v in rates["chat_overrides"].items() if k.casefold() in needle),
        rates["per_million_tokens"]["default_chat"],
    )
    cost = 0.0
    if prompt_tokens:
        cost += prompt_tokens * rate.get("text_input", 0.0) / 1_000_000
    if completion_tokens:
        cost += completion_tokens * rate.get("text_output", 0.0) / 1_000_000
    return round(cost, 6)
