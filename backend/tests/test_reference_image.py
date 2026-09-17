"""The uploaded packshot must actually reach the image provider.

Uploads are accepted before the campaign exists, so they are staged under
``_uploads/`` and associated with a campaign later. That split is easy to get
wrong in a way nothing else catches: the scene still generates, it is just
generated from a text description instead of from the real product, and the
identity anchor quietly weakens. These tests pin the lookup down.
"""

from __future__ import annotations

import io

from PIL import Image

from app.domain.enums import AssetFormat
from app.graph.nodes.images import _load_reference, master_scene_node
from app.domain.spec import CampaignSpec

from .test_contracts import _spec


def _png(colour=(10, 200, 30)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (256, 256), colour).save(buffer, "PNG")
    return buffer.getvalue()


def test_reference_found_in_the_staging_area(context, artifacts):
    """The realistic path: uploaded before the campaign id existed."""
    stored = artifacts.write(campaign_id="_uploads", data=_png(), media_type="image/png")
    spec = CampaignSpec.model_validate(
        _spec(product={**_spec()["product"], "reference_image_id": stored.artifact_id})
    )
    assert _load_reference(context, spec) == artifacts.read(stored.relative_path)


def test_reference_found_in_the_campaign_directory(context, artifacts):
    stored = artifacts.write(
        campaign_id=context.campaign_id, data=_png(), media_type="image/png"
    )
    spec = CampaignSpec.model_validate(
        _spec(product={**_spec()["product"], "reference_image_id": stored.artifact_id})
    )
    assert _load_reference(context, spec) is not None


def test_missing_reference_degrades_rather_than_crashing(context):
    """A deleted upload must not take the whole campaign down."""
    spec = CampaignSpec.model_validate(
        _spec(product={**_spec()["product"], "reference_image_id": "deadbeefdeadbeef"})
    )
    assert _load_reference(context, spec) is None


def test_no_reference_configured_returns_none(context):
    assert _load_reference(context, CampaignSpec.model_validate(_spec())) is None


async def test_packshot_switches_the_master_scene_to_an_edit_call(context, artifacts):
    """With a packshot, identity is anchored to the real asset via /images/edits
    with input_fidelity — not to a written description via /images/generations."""
    stored = artifacts.write(campaign_id="_uploads", data=_png(), media_type="image/png")
    spec = _spec(campaign_id=context.campaign_id)
    spec["product"]["reference_image_id"] = stored.artifact_id

    result = await master_scene_node({"spec": spec, "assets": {}}, context)
    asset = result["assets"][AssetFormat.MASTER_SCENE.value]

    assert context.providers.image.edit_calls == 1
    assert context.providers.image.generate_calls == 0
    assert asset["request"]["operation"] == "edits"
    assert asset["request"]["input_fidelity"] == "high"
    assert asset["request"]["source_asset_id"] == stored.artifact_id


async def test_without_a_packshot_the_master_scene_is_a_plain_generation(context):
    result = await master_scene_node(
        {"spec": _spec(campaign_id=context.campaign_id), "assets": {}}, context
    )
    asset = result["assets"][AssetFormat.MASTER_SCENE.value]

    assert context.providers.image.generate_calls == 1
    assert context.providers.image.edit_calls == 0
    assert asset["request"]["operation"] == "generations"
    assert asset["request"]["input_fidelity"] is None
