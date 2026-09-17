"""Content-addressed artifact store on the local filesystem.
"""

from __future__ import annotations
import hashlib
import secrets
from dataclasses import dataclass
from pathlib import Path
from ..config import get_settings

_MEDIA_EXTENSIONS: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "application/json": ".json",
}


def new_artifact_id() -> str:
    return secrets.token_hex(12)


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    artifact_id: str
    relative_path: str
    absolute_path: Path
    media_type: str
    byte_size: int
    sha256: str


class ArtifactStore:
    """Files live under ``<artifact_dir>/<campaign_id>/<artifact_id><ext>``.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root or get_settings().artifact_dir)

    @property
    def root(self) -> Path:
        return self._root

    def _campaign_dir(self, campaign_id: str) -> Path:
        safe = _safe_component(campaign_id)
        path = self._root / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    def write(
        self,
        *,
        campaign_id: str,
        data: bytes,
        media_type: str,
        artifact_id: str | None = None,
    ) -> StoredArtifact:
        aid = artifact_id or new_artifact_id()
        ext = _MEDIA_EXTENSIONS.get(media_type, ".bin")
        target = self._campaign_dir(campaign_id) / f"{aid}{ext}"
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(data)
        tmp.replace(target)

        return StoredArtifact(
            artifact_id=aid,
            relative_path=f"{_safe_component(campaign_id)}/{target.name}",
            absolute_path=target,
            media_type=media_type,
            byte_size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )

    def path_for(self, relative_path: str) -> Path:
        """Resolve a stored relative path, refusing anything escaping the root."""
        candidate = (self._root / relative_path).resolve()
        root = self._root.resolve()
        if not candidate.is_relative_to(root):
            raise ValueError(f"artifact path escapes the store: {relative_path!r}")
        return candidate

    def read(self, relative_path: str) -> bytes:
        return self.path_for(relative_path).read_bytes()

    def exists(self, relative_path: str) -> bool:
        try:
            return self.path_for(relative_path).is_file()
        except ValueError:
            return False

    def reserve_path(self, campaign_id: str, filename: str) -> Path:
        """A writable path for tools that produce files themselves (ffmpeg)."""
        return self._campaign_dir(campaign_id) / _safe_component(filename)


def _safe_component(value: str) -> str:
    """Reject path separators and traversal outright rather than sanitising.
    """
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise ValueError(f"unsafe path component: {value!r}")
    return value


_store: ArtifactStore | None = None


def get_artifact_store() -> ArtifactStore:
    global _store
    if _store is None:
        _store = ArtifactStore()
    return _store
