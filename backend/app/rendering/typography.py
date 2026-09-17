"""Font discovery and text fitting.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from PIL import ImageDraw, ImageFont

_BOLD_CANDIDATES = (
    r"C:\Windows\Fonts\segoeuib.ttf",
    r"C:\Windows\Fonts\arialbd.ttf",
    r"C:\Windows\Fonts\calibrib.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)

_REGULAR_CANDIDATES = (
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\calibri.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)


@lru_cache(maxsize=1)
def _resolve(bold: bool) -> str | None:
    for candidate in _BOLD_CANDIDATES if bold else _REGULAR_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


@lru_cache(maxsize=128)
def load_font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = _resolve(bold)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            pass
    return ImageFont.load_default(size=size)


def font_name() -> str:
    path = _resolve(bold=True)
    return Path(path).stem if path else "pillow-default"


@dataclass(frozen=True, slots=True)
class FittedText:
    lines: list[str]
    font: ImageFont.FreeTypeFont
    size: int
    width: int
    height: int
    line_height: int

    @property
    def is_empty(self) -> bool:
        return not self.lines


def _measure(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> tuple[int, int]:
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return right - left, bottom - top


def wrap_to_width(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    """Greedy word wrap. A single over-long word is allowed to overflow its line
    rather than being hyphenated — breaking a brand name mid-word looks worse."""
    words = text.split()
    if not words:
        return []

    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if _measure(draw, candidate, font)[0] <= max_width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def fit_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    *,
    max_width: int,
    max_height: int,
    max_size: int,
    min_size: int = 16,
    bold: bool = True,
    max_lines: int = 4,
    line_spacing: float = 1.18,
) -> FittedText:
    """Largest font size at which ``text`` fits the box after wrapping.
    """
    text = " ".join((text or "").split())
    if not text:
        return FittedText([], load_font(min_size, bold=bold), min_size, 0, 0, 0)

    size = max_size
    while size >= min_size:
        font = load_font(size, bold=bold)
        lines = wrap_to_width(draw, text, font, max_width)
        line_height = int(size * line_spacing)
        total_height = line_height * len(lines)
        widest = max((_measure(draw, line, font)[0] for line in lines), default=0)

        if len(lines) <= max_lines and total_height <= max_height and widest <= max_width:
            return FittedText(lines, font, size, widest, total_height, line_height)

        size = max(min_size, int(size * 0.92)) if size > min_size else min_size - 1

    font = load_font(min_size, bold=bold)
    lines = wrap_to_width(draw, text, font, max_width)[:max_lines]
    line_height = int(min_size * line_spacing)
    widest = max((_measure(draw, line, font)[0] for line in lines), default=0)
    return FittedText(lines, font, min_size, widest, line_height * len(lines), line_height)


def draw_lines(
    draw: ImageDraw.ImageDraw,
    fitted: FittedText,
    *,
    left: int,
    top: int,
    fill: tuple[int, int, int],
    align_center_width: int | None = None,
) -> int:
    """Draw fitted lines, returning the y coordinate just past the last one."""
    y = top
    for line in fitted.lines:
        x = left
        if align_center_width is not None:
            width = _measure(draw, line, fitted.font)[0]
            x = left + (align_center_width - width) // 2
        draw.text((x, y), line, font=fitted.font, fill=fill)
        y += fitted.line_height
    return y
