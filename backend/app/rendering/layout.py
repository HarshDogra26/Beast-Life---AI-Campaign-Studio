"""Deterministic composition: generated scene in, exact export target out.
"""

from __future__ import annotations
import io
from dataclasses import dataclass
from PIL import Image, ImageDraw, ImageFilter
from ..domain.assets import EXPORT_SIZES, Size
from ..domain.enums import AssetFormat, SizeStrategy
from ..domain.spec import CampaignSpec
from ..logging import get_logger
from . import palette as pal
from .typography import draw_lines, fit_text, load_font

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class LayoutPlan:
    """Where copy sits, as a fraction of the canvas.
    """

    margin: float
    headline_top: float
    headline_height: float
    headline_max_pt: float
    cta_bottom: float
    cta_height: float
    center_text: bool
    top_scrim: float
    bottom_scrim: float


LAYOUTS: dict[AssetFormat, LayoutPlan] = {
    AssetFormat.SQUARE: LayoutPlan(
        margin=0.068,
        headline_top=0.072,
        headline_height=0.235,
        headline_max_pt=0.092,
        cta_bottom=0.072,
        cta_height=0.085,
        center_text=True,
        top_scrim=0.38,
        bottom_scrim=0.30,
    ),
    AssetFormat.VERTICAL: LayoutPlan(
        margin=0.068,
        headline_top=0.088,
        headline_height=0.185,
        headline_max_pt=0.062,
        cta_bottom=0.085,
        cta_height=0.050,
        center_text=True,
        top_scrim=0.32,
        bottom_scrim=0.26,
    ),
}


def fit_to_export(
    image: Image.Image,
    target: Size,
    *,
    strategy: SizeStrategy,
    band_color: tuple[int, int, int],
) -> Image.Image:
    """Bring a generated image to the exact export size."""
    source = image.convert("RGB")

    if source.size == (target.width, target.height):
        return source

    same_aspect = abs(source.width / source.height - target.width / target.height) < 1e-6
    if same_aspect or strategy is SizeStrategy.NATIVE:
        if not same_aspect:
            log.warning(
                "layout.aspect_mismatch_on_native",
                source=f"{source.width}x{source.height}",
                target=str(target),
            )
            return _extend_canvas(source, target, band_color)
        return source.resize((target.width, target.height), Image.LANCZOS)

    return _extend_canvas(source, target, band_color)


def _extend_canvas(
    source: Image.Image, target: Size, band_color: tuple[int, int, int]
) -> Image.Image:
    """Scale to the target width, then extend vertically with graded bands.
    """
    scale = target.width / source.width
    scaled = source.resize(
        (target.width, max(1, round(source.height * scale))), Image.LANCZOS
    )

    if scaled.height >= target.height:
        top = (scaled.height - target.height) // 2
        return scaled.crop((0, top, target.width, top + target.height))

    canvas = Image.new("RGB", (target.width, target.height), band_color)
    deficit = target.height - scaled.height
    top_band = deficit // 2
    bottom_band = deficit - top_band

    # Blurred edge stretch under a colour wash.
    if top_band > 0:
        strip = scaled.crop((0, 0, target.width, min(48, scaled.height)))
        blurred = strip.resize((target.width, top_band), Image.LANCZOS).filter(
            ImageFilter.GaussianBlur(24)
        )
        canvas.paste(Image.blend(blurred, Image.new("RGB", blurred.size, band_color), 0.55), (0, 0))
    if bottom_band > 0:
        strip = scaled.crop((0, max(0, scaled.height - 48), target.width, scaled.height))
        blurred = strip.resize((target.width, bottom_band), Image.LANCZOS).filter(
            ImageFilter.GaussianBlur(24)
        )
        canvas.paste(
            Image.blend(blurred, Image.new("RGB", blurred.size, band_color), 0.55),
            (0, top_band + scaled.height),
        )

    canvas.paste(scaled, (0, top_band))
    return canvas


def _gradient_scrim(
    size: tuple[int, int], color: tuple[int, int, int], opacity: int, *, from_top: bool
) -> Image.Image:
    """A vertical alpha ramp. Softer and far less cheap-looking than a solid bar."""
    width, height = size
    mask = Image.new("L", (1, height))
    for y in range(height):
        t = y / max(height - 1, 1)
        if from_top:
            t = 1.0 - t
        mask.putpixel((0, y), int(opacity * (t**1.7)))
    layer = Image.new("RGBA", (width, height), (*color, 255))
    layer.putalpha(mask.resize((width, height), Image.BILINEAR))
    return layer


def build_plate(
    image_bytes: bytes,
    spec: CampaignSpec,
    fmt: AssetFormat,
    *,
    strategy: SizeStrategy,
) -> Image.Image:
    """The exactly-sized image with **no copy on it**.
    """
    target = EXPORT_SIZES[fmt]
    source = Image.open(io.BytesIO(image_bytes))
    band_color = pal.derive_band_color(source, spec.palette.background)
    canvas = fit_to_export(source, target, strategy=strategy, band_color=band_color)

    if canvas.size != (target.width, target.height):
        raise ValueError(
            f"layout produced {canvas.width}x{canvas.height}, expected {target}"
        )
    return canvas


def compose_ad(
    image_bytes: bytes,
    spec: CampaignSpec,
    fmt: AssetFormat,
    *,
    strategy: SizeStrategy,
) -> bytes:
    """Produce the final, exactly-sized ad with copy composited on top."""
    plan = LAYOUTS[fmt]
    canvas = build_plate(image_bytes, spec, fmt, strategy=strategy)
    canvas = _apply_scrims(canvas, plan, spec)
    _draw_copy(canvas, spec, fmt, plan)
    return encode_png(canvas)


def encode_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _apply_scrims(image: Image.Image, plan: LayoutPlan, spec: CampaignSpec) -> Image.Image:
    """Darken the copy zones enough to guarantee contrast, measured not guessed."""
    width, height = image.size
    layered = image.convert("RGBA")

    top_h = int(height * plan.top_scrim)
    bottom_h = int(height * plan.bottom_scrim)

    top_treatment = pal.choose_text_treatment(image, (0, 0, width, top_h))
    bottom_treatment = pal.choose_text_treatment(
        image, (0, height - bottom_h, width, height)
    )

    layered.alpha_composite(
        _gradient_scrim((width, top_h), top_treatment.scrim_color,
                        top_treatment.scrim_opacity, from_top=True),
        (0, 0),
    )
    layered.alpha_composite(
        _gradient_scrim((width, bottom_h), bottom_treatment.scrim_color,
                        bottom_treatment.scrim_opacity, from_top=False),
        (0, height - bottom_h),
    )

    log.info(
        "layout.scrims",
        top_contrast=round(top_treatment.measured_contrast, 2),
        bottom_contrast=round(bottom_treatment.measured_contrast, 2),
        top_opacity=top_treatment.scrim_opacity,
    )
    return layered.convert("RGB")


def _draw_copy(
    canvas: Image.Image, spec: CampaignSpec, fmt: AssetFormat, plan: LayoutPlan
) -> None:
    width, height = canvas.size
    draw = ImageDraw.Draw(canvas)
    margin = int(width * plan.margin)
    content_width = width - 2 * margin

    # -- headline (and optional subhead) --
    head_top = int(height * plan.headline_top)
    head_box_h = int(height * plan.headline_height)
    head_treatment = pal.choose_text_treatment(
        canvas, (margin, head_top, width - margin, head_top + head_box_h)
    )

    fitted = fit_text(
        draw,
        spec.headline,
        max_width=content_width,
        max_height=int(head_box_h * 0.68),
        max_size=int(height * plan.headline_max_pt),
        bold=True,
        max_lines=3,
    )
    y = draw_lines(
        draw,
        fitted,
        left=margin,
        top=head_top,
        fill=head_treatment.color,
        align_center_width=content_width if plan.center_text else None,
    )

    if spec.subhead:
        sub = fit_text(
            draw,
            spec.subhead,
            max_width=content_width,
            max_height=int(head_box_h * 0.30),
            max_size=max(18, int(fitted.size * 0.46)),
            bold=False,
            max_lines=2,
        )
        draw_lines(
            draw,
            sub,
            left=margin,
            top=y + int(fitted.size * 0.30),
            fill=head_treatment.color,
            align_center_width=content_width if plan.center_text else None,
        )

    # -- CTA pill --
    _draw_cta(canvas, draw, spec, plan, margin=margin)

    # -- product wordmark, small, opposite the CTA --
    brand_size = max(15, int(height * 0.019))
    brand_font = load_font(brand_size, bold=True)
    brand_treatment = pal.choose_text_treatment(
        canvas,
        (margin, height - int(height * 0.035), width - margin, height - int(height * 0.012)),
    )
    draw.text(
        (margin, height - int(height * 0.032)),
        spec.product.name.upper(),
        font=brand_font,
        fill=brand_treatment.color,
    )


def _draw_cta(
    canvas: Image.Image,
    draw: ImageDraw.ImageDraw,
    spec: CampaignSpec,
    plan: LayoutPlan,
    *,
    margin: int,
) -> None:
    width, height = canvas.size
    pill_h = int(height * plan.cta_height)
    pill_bottom = height - int(height * plan.cta_bottom)
    pill_top = pill_bottom - pill_h

    cta_font_size = max(16, int(pill_h * 0.40))
    font = load_font(cta_font_size, bold=True)
    bbox = draw.textbbox((0, 0), spec.cta_text, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]

    pad_x = int(pill_h * 0.75)
    pill_w = min(text_w + 2 * pad_x, width - 2 * margin)
    pill_left = (width - pill_w) // 2 if plan.center_text else margin

    accent = spec.palette.as_tuple("accent")
    on_accent = (
        pal.NEAR_BLACK
        if pal.contrast_ratio(pal.NEAR_BLACK, accent) > pal.contrast_ratio(pal.WHITE, accent)
        else pal.WHITE
    )

    draw.rounded_rectangle(
        [pill_left, pill_top, pill_left + pill_w, pill_bottom],
        radius=pill_h // 2,
        fill=accent,
    )
    draw.text(
        (
            pill_left + (pill_w - text_w) // 2 - bbox[0],
            pill_top + (pill_h - text_h) // 2 - bbox[1],
        ),
        spec.cta_text,
        font=font,
        fill=on_accent,
    )
