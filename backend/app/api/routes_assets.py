"""Asset preview and download.
"""

from __future__ import annotations
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse
from ..logging import get_logger
from ..storage.artifacts import get_artifact_store
from ..storage.db import session_scope
from ..storage.repo import AssetRepository

log = get_logger(__name__)
router = APIRouter(prefix="/api/assets", tags=["assets"])

_EXTENSION = {"image/png": "png", "image/jpeg": "jpg", "video/mp4": "mp4"}


async def _resolve(asset_id: str):
    async with session_scope() as session:
        asset = await AssetRepository(session).get(asset_id)
    if asset is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"asset {asset_id} not found")

    store = get_artifact_store()
    try:
        path = store.path_for(asset.artifact_path)
    except ValueError as exc:
        log.error("asset.path_escape", asset_id=asset_id, path=asset.artifact_path)
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid artifact path") from exc

    if not path.is_file():
        raise HTTPException(
            status.HTTP_410_GONE,
            detail={
                "error": "The artifact file is missing from the store.",
                "code": "artifact_missing",
                "detail": {"asset_id": asset_id, "expected_path": asset.artifact_path},
            },
        )
    return asset, path


@router.get("/{asset_id}/preview")
async def preview_asset(asset_id: str) -> FileResponse:
    """Inline preview. Videos stream, so range requests matter for playback."""
    asset, path = await _resolve(asset_id)
    return FileResponse(
        path,
        media_type=asset.media_type,
        headers={
            "Content-Disposition": "inline",
            "Cache-Control": "public, max-age=31536000, immutable",
            "Accept-Ranges": "bytes",
        },
    )


@router.get("/{asset_id}/download")
async def download_asset(asset_id: str) -> FileResponse:
    asset, path = await _resolve(asset_id)
    extension = _EXTENSION.get(asset.media_type, "bin")
    filename = f"{asset.campaign_id}_{asset.format}_{asset.width}x{asset.height}.{extension}"
    return FileResponse(
        path,
        media_type=asset.media_type,
        filename=filename,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )
