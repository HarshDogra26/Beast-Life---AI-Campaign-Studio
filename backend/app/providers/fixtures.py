"""Deterministic offline providers for review without keys or network.
"""

from __future__ import annotations
import hashlib
import io
import json
import math
import random
from typing import Any
from PIL import Image, ImageDraw, ImageFilter
from .base import (
    ChatTurn,
    ImageProvider,
    ImageResult,
    LLMProvider,
    LLMResult,
    LLMUsage,
    ProviderError,
    ToolCall,
    ToolSchema,
)
from .capabilities import ImageModelCapabilities, resolve_capabilities


def _seed_from(text: str) -> int:
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")


class FixtureLLMProvider(LLMProvider):
    """Scripted but genuinely interactive chat model."""

    name = "fixture_llm"
    model = "fixture-chat"

    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self._fail_on = fail_on or set()

    # -- tool-calling loop -------------------------------------------------

    async def chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[ToolSchema] | None = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> ChatTurn:
        tool_names = {t.name for t in (tools or [])}
        assistant_turns = sum(1 for m in messages if m.get("role") == "assistant")
        seen_urls = self._urls_seen(messages)
        fetched = self._urls_fetched(messages)

        # Turn 1: decide what to search for.
        if assistant_turns == 0 and "web_search" in tool_names:
            return self._turn(
                "Starting broad: I need independent evidence about how this audience "
                "actually behaves before proposing angles.",
                "web_search",
                {"query": "busy gym-goers protein supplement habits convenience", "max_results": 5},
            )

        # Turns 2-3: read the most promising results.
        unread = [u for u in seen_urls if u not in fetched]
        if unread and len(fetched) < 2 and "fetch_page" in tool_names:
            return self._turn(
                f"The search returned {len(seen_urls)} candidates; reading "
                f"{unread[0]} because the snippet suggests primary survey data "
                "rather than vendor marketing copy.",
                "fetch_page",
                {"url": unread[0]},
            )

        # Turn 4: a deliberate follow-up search, as the brief requires.
        if len(fetched) == 2 and not self._did_second_search(messages) and "web_search" in tool_names:
            return self._turn(
                "Two sources cover motivation but neither covers timing or routine. "
                "Running a second, narrower search to close that gap.",
                "web_search",
                {"query": "gym goers post workout nutrition timing routine study", "max_results": 5},
            )

        # Turn 5: read one more page to reach the 3-source floor.
        if unread and len(fetched) < 3 and "fetch_page" in tool_names:
            return self._turn(
                f"Reading {unread[0]} to reach at least three independent sources "
                "before I synthesise anything.",
                "fetch_page",
                {"url": unread[0]},
            )

        return ChatTurn(
            content=(
                f"I have {len(fetched)} sources covering motivation, convenience and "
                "routine timing. That is enough evidence to separate what is sourced "
                "from what is my interpretation. Stopping here."
            ),
            tool_calls=[],
            finish_reason="stop",
            usage=LLMUsage(prompt_tokens=800, completion_tokens=120),
            model=self.model,
        )

    def _turn(self, summary: str, tool: str, args: dict[str, Any]) -> ChatTurn:
        return ChatTurn(
            content=summary,
            tool_calls=[ToolCall(id=f"call_{tool}_{len(args)}", name=tool, arguments=args)],
            finish_reason="tool_calls",
            usage=LLMUsage(prompt_tokens=600, completion_tokens=80),
            model=self.model,
        )

    @staticmethod
    def _urls_seen(messages: list[dict[str, Any]]) -> list[str]:
        """Pull candidate URLs out of prior tool results, preserving order."""
        urls: list[str] = []
        for message in messages:
            if message.get("role") != "tool":
                continue
            try:
                payload = json.loads(message.get("content") or "{}")
            except json.JSONDecodeError:
                continue
            for hit in payload.get("results", []):
                url = hit.get("url")
                if url and url not in urls:
                    urls.append(url)
        return urls

    @staticmethod
    def _urls_fetched(messages: list[dict[str, Any]]) -> list[str]:
        fetched: list[str] = []
        for message in messages:
            if message.get("role") != "tool":
                continue
            try:
                payload = json.loads(message.get("content") or "{}")
            except json.JSONDecodeError:
                continue
            if payload.get("kind") == "page" and (url := payload.get("url")):
                fetched.append(url)
        return fetched

    @staticmethod
    def _did_second_search(messages: list[dict[str, Any]]) -> bool:
        searches = 0
        for message in messages:
            for call in message.get("tool_calls") or []:
                if (call.get("function") or {}).get("name") == "web_search":
                    searches += 1
        return searches >= 2

    # -- structured generation --------------------------------------------

    async def complete(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict[str, Any] | None = None,
        temperature: float = 0.7,
        max_tokens: int = 4096,
    ) -> LLMResult:
        title = (json_schema or {}).get("title", "")
        if title in self._fail_on:
            raise ProviderError(f"fixture failure injected for {title}")

        if title == "AngleProposals":
            payload = self._angles(user)
        elif title == "CampaignSpecDraft":
            payload = self._spec(user)
        else:
            payload = {"text": "fixture response"}

        return LLMResult(
            text=json.dumps(payload),
            usage=LLMUsage(prompt_tokens=1200, completion_tokens=900),
            model=self.model,
        )

    @staticmethod
    def _source_ids(user: str) -> list[str]:
        """Cite only ids that actually appear in the supplied evidence block.
        """
        import re

        found = sorted(set(re.findall(r'id="(s\d{1,3})"', user)), key=lambda s: int(s[1:]))
        return found or ["s1"]

    def _angles(self, user: str) -> dict[str, Any]:
        ids = self._source_ids(user)
        primary, secondary, tertiary = (ids + ids + ids)[:3]
        return {
            "angles": [
                {
                    "id": "a1",
                    "title": "The 90-second window",
                    "audience_insight": (
                        "Sourced reporting describes gym-goers leaving straight after "
                        "training and treating post-session nutrition as a logistics "
                        "problem rather than a ritual."
                    ),
                    "insight_source_ids": [primary],
                    "hook": "Straight from the rack to the door.",
                    "visual_direction": (
                        "Tight handheld framing in a gym doorway at golden hour, the tub "
                        "held mid-stride, warm rim light, motion implied by a soft "
                        "background blur rather than by the subject."
                    ),
                    "rationale": (
                        "Leaning into the exit moment rather than the workout gives us a "
                        "visual nobody else in the category owns, and it reframes the "
                        "product as part of leaving rather than part of training."
                    ),
                    "supporting_source_ids": [primary, secondary],
                },
                {
                    "id": "a2",
                    "title": "Built for the bag, not the shelf",
                    "audience_insight": (
                        "Sources note that storage and portability shape repeat purchase "
                        "for people who train away from home."
                    ),
                    "insight_source_ids": [secondary],
                    "hook": "Fits the bag you already carry.",
                    "visual_direction": (
                        "Overhead flat-lay of an open gym bag on a locker bench, the tub "
                        "nested beside a towel and keys, cool overhead light, tight and "
                        "orderly composition with generous negative space above."
                    ),
                    "rationale": (
                        "A flat-lay reads instantly at feed scale and lets the packaging "
                        "carry the frame, which suits a product introduction where "
                        "recognition matters more than atmosphere."
                    ),
                    "supporting_source_ids": [secondary, tertiary],
                },
                {
                    "id": "a3",
                    "title": "The unremarkable routine",
                    "audience_insight": (
                        "Reporting on training consistency describes habit and repetition "
                        "as the factor people credit, rather than intensity."
                    ),
                    "insight_source_ids": [tertiary],
                    "hook": "Same time. Same shelf. Same scoop.",
                    "visual_direction": (
                        "A calm kitchen counter at early morning, the tub in its habitual "
                        "place, soft directional daylight from the left, muted palette and "
                        "a deliberately static composition."
                    ),
                    "rationale": (
                        "Quiet consistency is an under-used register in a category that "
                        "defaults to shouting, and it gives the video a natural rhythm to "
                        "cut against."
                    ),
                    "supporting_source_ids": [tertiary, primary],
                },
            ]
        }

    @staticmethod
    def _spec(user: str) -> dict[str, Any]:
        return {
            "headline": "Straight from the rack to the door",
            "subhead": "Post-session nutrition that keeps up with your exit",
            "hook": "Straight from the rack to the door.",
            "product": {
                "form_factor": "matte charcoal 1kg tub with a wide screw lid",
                "packaging_description": (
                    "A matte charcoal cylindrical tub with a wide screw lid, a single "
                    "bold wordmark across the front and a narrow accent band near the base."
                ),
            },
            "scene": {
                "setting": "the doorway of a city gym in late afternoon light",
                "subject": "the product tub held mid-stride by a person leaving",
                "lighting": "warm low-angle golden hour with a soft rim highlight",
                "mood": "practical, unhurried, energetic",
                "props": ["gym bag strap", "towel"],
            },
            "palette": {
                "primary": "#1B1F24",
                "secondary": "#E8E4DC",
                "accent": "#E2603B",
                "background": "#0E1114",
            },
            "composition": {
                "square": {
                    "framing": "Mid-shot with the tub centred and the doorway framing it symmetrically.",
                    "product_placement": "centre, occupying the middle third",
                    "text_safe_zone": "upper fifth and lower fifth kept calm and uncluttered",
                },
                "vertical": {
                    "framing": "Full-height composition with the figure low and open sky-lit wall above.",
                    "product_placement": "lower third, held at hip height",
                    "text_safe_zone": "top quarter for the headline, bottom sixth for the CTA",
                },
            },
            "video": {
                "beats": [
                    {
                        "label": "hook",
                        "seconds": 2.6,
                        "on_screen_text": "Straight from the rack to the door",
                        "motion": "slow push in on the doorway",
                    },
                    {
                        "label": "product",
                        "seconds": 3.0,
                        "on_screen_text": "Post-session nutrition that keeps up",
                        "motion": "gentle drift across the tub",
                    },
                    {
                        "label": "cta",
                        "seconds": 2.4,
                        "on_screen_text": "Explore the range",
                        "motion": "settle and hold on the product",
                    },
                ],
                "transition": "crossfade",
            },
        }


class FixtureImageProvider(ImageProvider):
    """Renders a plausible studio scene at exactly the requested size.
    """

    name = "fixture_image"

    def __init__(self, *, model: str = "fixture-image", fail_on: set[str] | None = None) -> None:
        self.model = model
        self.capabilities: ImageModelCapabilities = resolve_capabilities("gpt-image-2.5-sunburst")
        self._fail_on = fail_on or set()

    async def generate(self, *, prompt: str, size: str, quality: str) -> ImageResult:
        return self._render(prompt=prompt, size=size, anchor=None)

    async def edit(
        self,
        *,
        prompt: str,
        size: str,
        quality: str,
        images: list[tuple[str, bytes, str]],
        input_fidelity: str | None = None,
    ) -> ImageResult:
        anchor = images[0][1] if images else None
        return self._render(prompt=prompt, size=size, anchor=anchor)

    def _render(self, *, prompt: str, size: str, anchor: bytes | None) -> ImageResult:
        width, height = (int(v) for v in size.lower().split("x"))
        rng = random.Random(_seed_from(prompt[-400:] if anchor else prompt[:400]))

        if anchor is not None:
            canvas = self._reframe(anchor, width, height)
        else:
            canvas = self._scene(width, height, rng)

        buffer = io.BytesIO()
        canvas.save(buffer, format="PNG", optimize=True)
        return ImageResult(
            data=buffer.getvalue(),
            media_type="image/png",
            model=self.model,
            input_tokens=1200 if anchor else 400,
            output_tokens=int(width * height / 1000),
        )

    @staticmethod
    def _scene(width: int, height: int, rng: random.Random) -> Image.Image:
        base_hue = rng.randint(0, 40)
        top = (18 + base_hue // 2, 20 + base_hue // 3, 26 + base_hue // 2)
        bottom = (200 - base_hue, 150 - base_hue // 2, 110)

        canvas = Image.new("RGB", (width, height), top)
        draw = ImageDraw.Draw(canvas)
        for y in range(height):
            t = y / max(height - 1, 1)
            eased = t * t * (3 - 2 * t)
            draw.line(
                [(0, y), (width, y)],
                fill=tuple(int(top[i] + (bottom[i] - top[i]) * eased) for i in range(3)),
            )

        glow = Image.new("L", (width, height), 0)
        gdraw = ImageDraw.Draw(glow)
        cx, cy = int(width * 0.62), int(height * 0.34)
        radius = int(min(width, height) * 0.42)
        gdraw.ellipse([cx - radius, cy - radius, cx + radius, cy + radius], fill=150)
        glow = glow.filter(ImageFilter.GaussianBlur(radius // 3))
        canvas = Image.composite(Image.new("RGB", (width, height), (255, 238, 210)), canvas, glow)
        tub_w = int(width * 0.30)
        tub_h = int(min(height * 0.34, tub_w * 1.45))
        left = (width - tub_w) // 2
        top_y = int(height * 0.60) - tub_h // 2
        body = [left, top_y, left + tub_w, top_y + tub_h]

        shadow = Image.new("L", (width, height), 0)
        ImageDraw.Draw(shadow).ellipse(
            [left - tub_w // 6, top_y + tub_h - tub_h // 8,
             left + tub_w + tub_w // 6, top_y + tub_h + tub_h // 5],
            fill=120,
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(max(6, tub_w // 12)))
        canvas = Image.composite(Image.new("RGB", (width, height), (10, 10, 12)), canvas, shadow)

        draw = ImageDraw.Draw(canvas)
        draw.rounded_rectangle(body, radius=tub_w // 10, fill=(28, 31, 36))
        draw.ellipse(
            [left, top_y - tub_h // 14, left + tub_w, top_y + tub_h // 14],
            fill=(46, 50, 57),
        )
        accent_y = top_y + int(tub_h * 0.76)
        draw.rectangle(
            [left, accent_y, left + tub_w, accent_y + max(4, tub_h // 22)],
            fill=(226, 96, 59),
        )
        draw.line([left + tub_w // 12, top_y + tub_h // 8,
                   left + tub_w // 12, top_y + tub_h - tub_h // 8],
                  fill=(90, 95, 104), width=max(2, tub_w // 40))

        grain = Image.effect_noise((width, height), 8).convert("L")
        return Image.blend(canvas, Image.merge("RGB", (grain, grain, grain)), 0.045)

    @staticmethod
    def _reframe(anchor: bytes, width: int, height: int) -> Image.Image:
        """Re-compose the anchor into a new aspect, preserving its identity.
        """
        source = Image.open(io.BytesIO(anchor)).convert("RGB")
        scale = max(width / source.width, height / source.height)
        resized = source.resize(
            (math.ceil(source.width * scale), math.ceil(source.height * scale)),
            Image.LANCZOS,
        )
        x = (resized.width - width) // 2
        y = min(int((resized.height - height) * 0.62), max(resized.height - height, 0))
        return resized.crop((x, y, x + width, y + height))
