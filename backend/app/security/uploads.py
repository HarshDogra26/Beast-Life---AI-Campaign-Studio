"""Upload validation.
"""

from __future__ import annotations
import io
from dataclasses import dataclass
from PIL import Image, UnidentifiedImageError

_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"RIFF", "image/webp"),
)

MAX_PIXELS = 50_000_000


class UploadRejected(ValueError):
    """Upload failed validation. The message is safe to return to the client."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    data: bytes
    media_type: str
    width: int
    height: int


def sniff_media_type(data: bytes) -> str | None:
    for prefix, media_type in _MAGIC:
        if not data.startswith(prefix):
            continue
        if media_type == "image/webp":
            # RIFF is a container; confirm the WEBP fourcc before accepting.
            if len(data) >= 12 and data[8:12] == b"WEBP":
                return media_type
            return None
        return media_type
    return None


def validate_image_upload(
    data: bytes,
    *,
    declared_type: str | None,
    allowed_types: frozenset[str],
    max_bytes: int,
) -> ValidatedUpload:
    """Validate and normalise an uploaded reference image."""
    if not data:
        raise UploadRejected("The uploaded file is empty.", code="empty_file")
    if len(data) > max_bytes:
        raise UploadRejected(
            f"File is {len(data) / 1_048_576:.1f} MB; the limit is "
            f"{max_bytes / 1_048_576:.1f} MB.",
            code="too_large",
        )

    sniffed = sniff_media_type(data)
    if sniffed is None:
        raise UploadRejected(
            "File content is not a PNG, JPEG or WebP image, whatever its name or "
            "declared type says.",
            code="bad_magic",
        )
    if sniffed not in allowed_types:
        raise UploadRejected(
            f"{sniffed} is not an accepted type ({', '.join(sorted(allowed_types))}).",
            code="type_not_allowed",
        )
    if declared_type and declared_type.split(";")[0].strip() != sniffed:
        # Not fatal — browsers get this wrong — but worth surfacing.
        declared_type = sniffed

    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()
        with Image.open(io.BytesIO(data)) as image:
            if image.width * image.height > MAX_PIXELS:
                raise UploadRejected(
                    f"Image is {image.width}x{image.height}, which exceeds the "
                    f"{MAX_PIXELS:,} pixel limit.",
                    code="too_many_pixels",
                )
            normalised = image.convert("RGB")
            buffer = io.BytesIO()
            normalised.save(buffer, format="PNG", optimize=True)
            return ValidatedUpload(
                data=buffer.getvalue(),
                media_type="image/png",
                width=normalised.width,
                height=normalised.height,
            )
    except UploadRejected:
        raise
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise UploadRejected(
            f"The file could not be decoded as an image: {exc}", code="undecodable"
        ) from exc
