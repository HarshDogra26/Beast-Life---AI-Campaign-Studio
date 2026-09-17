"""The size arithmetic that reaches the 1080-px export targets.

This is the least obvious correctness property in the project: Azure's image
models cannot emit a 1080-px edge at all, so hitting the assignment's export
sizes depends on picking generation sizes whose *aspect* is exact and then
downscaling. These tests pin that reasoning down so a future edit to the size
table cannot quietly reintroduce a crop or a stretch.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.domain.assets import (
    EXPORT_SIZES,
    LEGACY_SIZES,
    MASTER_SCENE_SIZE,
    MAX_PIXELS,
    MIN_PIXELS,
    GenerationRequest,
    RenderedAsset,
    Size,
    generation_size,
    is_exact_aspect,
    validate_custom_size,
)
from app.domain.enums import AssetFormat, SizeStrategy
from app.rendering.layout import compose_ad, fit_to_export

from .test_contracts import _spec
from app.domain.spec import CampaignSpec

IMAGE_FORMATS = [AssetFormat.SQUARE, AssetFormat.VERTICAL]


def test_export_targets_match_the_assignment():
    assert (EXPORT_SIZES[AssetFormat.SQUARE].width, EXPORT_SIZES[AssetFormat.SQUARE].height) == (1080, 1080)
    assert (EXPORT_SIZES[AssetFormat.VERTICAL].width, EXPORT_SIZES[AssetFormat.VERTICAL].height) == (1080, 1920)


def test_1080_is_not_generatable_which_is_why_this_module_exists():
    """The premise: 1080 is not a multiple of 16, so no custom size can be 1080."""
    assert 1080 % 16 != 0
    assert validate_custom_size(Size(width=1080, height=1920))
    assert (1080, 1920) not in LEGACY_SIZES


@pytest.mark.parametrize("fmt", IMAGE_FORMATS)
def test_native_sizes_are_legal_custom_sizes(fmt):
    size = generation_size(fmt, SizeStrategy.NATIVE)
    assert validate_custom_size(size) == [], f"{size} violates Azure's rules"
    assert size.width % 16 == 0 and size.height % 16 == 0
    assert MIN_PIXELS <= size.pixels <= MAX_PIXELS


@pytest.mark.parametrize("fmt", IMAGE_FORMATS)
def test_native_generation_aspect_is_exact(fmt):
    """The whole point: an exact aspect means a pure downscale, no crop."""
    assert is_exact_aspect(fmt, SizeStrategy.NATIVE)
    assert generation_size(fmt, SizeStrategy.NATIVE).aspect == EXPORT_SIZES[fmt].aspect


@pytest.mark.parametrize("fmt", IMAGE_FORMATS)
def test_native_generation_is_larger_than_the_target(fmt):
    """Downscaling preserves detail; upscaling invents it."""
    generated, target = generation_size(fmt, SizeStrategy.NATIVE), EXPORT_SIZES[fmt]
    assert generated.width >= target.width and generated.height >= target.height


def test_vertical_uses_the_smallest_valid_exact_9_16_size():
    """Exact 9:16 with both edges divisible by 16 requires w=144m, h=256m.

    m=7 (1008x1792) would force an upscale; m=8 (1152x2048) is the first that
    does not. Larger m is legal but costs more output tokens for no benefit.
    """
    chosen = generation_size(AssetFormat.VERTICAL, SizeStrategy.NATIVE)
    assert (chosen.width, chosen.height) == (1152, 2048)
    assert chosen.aspect_label == "9:16"

    smaller = Size(width=144 * 7, height=256 * 7)
    assert validate_custom_size(smaller) == []          # legal...
    assert smaller.width < EXPORT_SIZES[AssetFormat.VERTICAL].width  # ...but an upscale


@pytest.mark.parametrize("fmt", IMAGE_FORMATS)
def test_bands_sizes_are_legal_for_the_legacy_model_family(fmt):
    size = generation_size(fmt, SizeStrategy.BANDS)
    assert (size.width, size.height) in LEGACY_SIZES


def test_master_scene_size_is_valid_under_both_strategies():
    """The identity anchor must be identical whichever path is taken."""
    assert validate_custom_size(MASTER_SCENE_SIZE) == []
    assert (MASTER_SCENE_SIZE.width, MASTER_SCENE_SIZE.height) in LEGACY_SIZES


def test_bands_vertical_is_not_exact_aspect_hence_canvas_extension():
    assert not is_exact_aspect(AssetFormat.VERTICAL, SizeStrategy.BANDS)


@pytest.mark.parametrize("strategy", [SizeStrategy.NATIVE, SizeStrategy.BANDS])
@pytest.mark.parametrize("fmt", IMAGE_FORMATS)
def test_fit_to_export_produces_exact_target_pixels(fmt, strategy):
    """Both strategies land on the exact export size, measured on real pixels."""
    generated = generation_size(fmt, strategy)
    source = Image.new("RGB", (generated.width, generated.height), (90, 110, 130))
    result = fit_to_export(
        source, EXPORT_SIZES[fmt], strategy=strategy, band_color=(20, 20, 24)
    )
    assert result.size == (EXPORT_SIZES[fmt].width, EXPORT_SIZES[fmt].height)


@pytest.mark.parametrize("strategy", [SizeStrategy.NATIVE, SizeStrategy.BANDS])
@pytest.mark.parametrize("fmt", IMAGE_FORMATS)
def test_compose_ad_output_is_exactly_the_export_size(fmt, strategy):
    """End-to-end through the real layout engine, including text overlay."""
    spec = CampaignSpec.model_validate(_spec())
    generated = generation_size(fmt, strategy)
    buffer = io.BytesIO()
    Image.new("RGB", (generated.width, generated.height), (120, 96, 70)).save(buffer, "PNG")

    composed = compose_ad(buffer.getvalue(), spec, fmt, strategy=strategy)
    with Image.open(io.BytesIO(composed)) as image:
        assert image.size == (EXPORT_SIZES[fmt].width, EXPORT_SIZES[fmt].height)
    assert len(composed) > 5_000


def test_generation_request_refuses_an_illegal_custom_size():
    """A request that cannot succeed never reaches the network."""
    with pytest.raises(ValueError, match="not a legal custom size"):
        GenerationRequest(
            format=AssetFormat.SQUARE,
            operation="generations",
            model="gpt-image-2.5-sunburst",
            prompt="x",
            generated_size=Size(width=1080, height=1080),  # not a multiple of 16
            export_size=EXPORT_SIZES[AssetFormat.SQUARE],
            strategy=SizeStrategy.NATIVE,
        )


def test_generation_request_refuses_a_custom_size_on_the_legacy_path():
    with pytest.raises(ValueError, match="legacy size set"):
        GenerationRequest(
            format=AssetFormat.VERTICAL,
            operation="edits",
            model="gpt-image-1",
            prompt="x",
            generated_size=Size(width=1152, height=2048),
            export_size=EXPORT_SIZES[AssetFormat.VERTICAL],
            strategy=SizeStrategy.BANDS,
        )


def _asset(**overrides) -> dict:
    return {
        "id": "a" * 16,
        "campaign_id": "c" * 16,
        "format": AssetFormat.VERTICAL,
        "artifact_path": "c/x.png",
        "media_type": "image/png",
        "width": 1080,
        "height": 1920,
        "byte_size": 1000,
        "sha256": "0" * 64,
        "request": GenerationRequest(
            format=AssetFormat.VERTICAL,
            operation="edits",
            model="m",
            prompt="p",
            generated_size=generation_size(AssetFormat.VERTICAL, SizeStrategy.NATIVE),
            export_size=EXPORT_SIZES[AssetFormat.VERTICAL],
            strategy=SizeStrategy.NATIVE,
        ),
    } | overrides


def test_rendered_asset_rejects_wrong_dimensions():
    """A mis-sized asset is a failure, not something to ship quietly."""
    with pytest.raises(ValueError, match="must be exactly"):
        RenderedAsset.model_validate(_asset(width=1024, height=1820))


def test_video_asset_requires_a_duration_in_range():
    base = _asset(format=AssetFormat.VIDEO, media_type="video/mp4")
    with pytest.raises(ValueError, match="require a measured duration"):
        RenderedAsset.model_validate(base)
    with pytest.raises(ValueError, match="outside the required 6-10s"):
        RenderedAsset.model_validate(base | {"duration_s": 14.0})
    assert RenderedAsset.model_validate(base | {"duration_s": 8.0}).duration_s == 8.0
