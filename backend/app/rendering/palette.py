"""Colour sampling and contrast.
"""

from __future__ import annotations
from dataclasses import dataclass
from PIL import Image

RGB = tuple[int, int, int]

MIN_CONTRAST = 4.5

WHITE: RGB = (255, 255, 255)
NEAR_BLACK: RGB = (17, 19, 22)


def hex_to_rgb(value: str) -> RGB:
    value = value.lstrip("#")
    return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))


def rgb_to_hex(rgb: RGB) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _channel_luminance(channel: int) -> float:
    c = channel / 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb: RGB) -> float:
    r, g, b = (_channel_luminance(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a: RGB, b: RGB) -> float:
    la, lb = relative_luminance(a), relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def average_color(image: Image.Image, box: tuple[int, int, int, int]) -> RGB:
    """Mean colour of a region, used to decide text polarity."""
    left, top, right, bottom = box
    left, top = max(0, left), max(0, top)
    right, bottom = min(image.width, right), min(image.height, bottom)
    if right <= left or bottom <= top:
        return NEAR_BLACK
    region = image.crop((left, top, right, bottom)).convert("RGB")
    return region.resize((1, 1), Image.BOX).getpixel((0, 0))  # type: ignore[return-value]


def dominant_colors(image: Image.Image, *, count: int = 5) -> list[RGB]:
    """Quantised palette of an image, most frequent first."""
    small = image.convert("RGB").resize((160, 160), Image.BILINEAR)
    quantised = small.quantize(colors=max(count, 2), method=Image.Quantize.MEDIANCUT)
    palette = quantised.getpalette() or []
    ranked = sorted(quantised.getcolors() or [], key=lambda pair: -pair[0])
    out: list[RGB] = []
    for _, index in ranked[:count]:
        base = index * 3
        if base + 2 < len(palette):
            out.append((palette[base], palette[base + 1], palette[base + 2]))
    return out or [NEAR_BLACK]


@dataclass(frozen=True, slots=True)
class TextTreatment:
    """A measured decision about how to render text at one location."""

    color: RGB
    scrim_color: RGB
    scrim_opacity: int
    measured_contrast: float

    @property
    def passes(self) -> bool:
        return self.measured_contrast >= MIN_CONTRAST


def choose_text_treatment(
    image: Image.Image,
    box: tuple[int, int, int, int],
    *,
    preferred: RGB | None = None,
) -> TextTreatment:
    """Pick a text colour and, if needed, a scrim strong enough to make it legible.
    """
    backdrop = average_color(image, box)
    candidates: list[RGB] = [preferred] if preferred else []
    candidates += [WHITE, NEAR_BLACK]

    best = max(candidates, key=lambda c: contrast_ratio(c, backdrop))
    ratio = contrast_ratio(best, backdrop)
    if ratio >= MIN_CONTRAST:
        return TextTreatment(best, _scrim_for(best), 70, ratio)

    scrim = _scrim_for(best)
    for opacity in range(90, 246, 15):
        blended = _blend(backdrop, scrim, opacity / 255)
        ratio = contrast_ratio(best, blended)
        if ratio >= MIN_CONTRAST:
            return TextTreatment(best, scrim, opacity, ratio)

    return TextTreatment(best, scrim, 245, contrast_ratio(best, _blend(backdrop, scrim, 0.96)))


def _scrim_for(text_color: RGB) -> RGB:
    """A scrim opposite the text, so it always increases separation."""
    return NEAR_BLACK if relative_luminance(text_color) > 0.5 else (245, 245, 245)


def _blend(base: RGB, overlay: RGB, alpha: float) -> RGB:
    return tuple(  
        round(base[i] * (1 - alpha) + overlay[i] * alpha) for i in range(3)
    )


def derive_band_color(image: Image.Image, fallback: str) -> RGB:
    """Pick a band colour from the image's own palette.
    """
    colors = dominant_colors(image, count=5)
    darkest = min(colors, key=relative_luminance)
    if relative_luminance(darkest) < 0.02:
        return hex_to_rgb(fallback)
    return darkest
