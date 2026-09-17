"""Programmatic video render: Pillow keyframes, FFmpeg motion, ffprobe verification.
"""

from __future__ import annotations
import asyncio
import io
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from PIL import Image, ImageDraw
from ..domain.spec import CampaignSpec
from ..logging import get_logger
from . import palette as pal
from .layout import _gradient_scrim  # shared so video and stills match exactly
from .typography import draw_lines, fit_text, load_font

log = get_logger(__name__)

WIDTH, HEIGHT = 1080, 1920
FPS = 30
TRANSITION_S = 0.5
SUPERSAMPLE = 1.5


class FFmpegMissingError(RuntimeError):
    """ffmpeg or ffprobe is not on PATH."""

    kind = "ffmpeg_missing"


class VideoRenderError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "video_render_failed") -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True, slots=True)
class ProbeResult:
    width: int
    height: int
    duration_s: float
    codec: str


def check_ffmpeg(ffmpeg_bin: str, ffprobe_bin: str) -> None:
    """Preflight. Reports a clear cause instead of a cryptic mid-render crash."""
    missing = [b for b in (ffmpeg_bin, ffprobe_bin) if shutil.which(b) is None]
    if missing:
        raise FFmpegMissingError(
            f"not found on PATH: {', '.join(missing)}. "
            "Install FFmpeg (winget install Gyan.FFmpeg) or set FFMPEG_BIN/FFPROBE_BIN."
        )


# --------------------------------------------------------------------------
# keyframes
# --------------------------------------------------------------------------


def build_keyframes(vertical_asset: bytes, spec: CampaignSpec) -> list[Image.Image]:
    """One frame per beat, composed from the approved vertical ad.
    """
    base = Image.open(io.BytesIO(vertical_asset)).convert("RGB")
    if base.size != (WIDTH, HEIGHT):
        base = base.resize((WIDTH, HEIGHT), Image.LANCZOS)

    frames: list[Image.Image] = []
    last_index = len(spec.video.beats) - 1
    for index, beat in enumerate(spec.video.beats):
        frames.append(
            _end_card(base, spec, beat.on_screen_text)
            if index == last_index
            else _beat_frame(base, spec, beat.on_screen_text, emphasis=index == 0)
        )
    return frames


def _clean_plate(base: Image.Image) -> Image.Image:
    """Lay a readability scrim over the copy zones.
    """
    plate = base.convert("RGBA")
    top = _gradient_scrim((WIDTH, int(HEIGHT * 0.36)), pal.NEAR_BLACK, 150, from_top=True)
    bottom = _gradient_scrim((WIDTH, int(HEIGHT * 0.26)), pal.NEAR_BLACK, 130, from_top=False)
    plate.alpha_composite(top, (0, 0))
    plate.alpha_composite(bottom, (0, HEIGHT - bottom.height))
    return plate.convert("RGB")


def _beat_frame(
    base: Image.Image, spec: CampaignSpec, text: str, *, emphasis: bool
) -> Image.Image:
    frame = _clean_plate(base)
    draw = ImageDraw.Draw(frame)
    margin = int(WIDTH * 0.08)
    content = WIDTH - 2 * margin

    fitted = fit_text(
        draw,
        text or spec.headline,
        max_width=content,
        max_height=int(HEIGHT * (0.26 if emphasis else 0.18)),
        max_size=int(HEIGHT * (0.070 if emphasis else 0.050)),
        bold=True,
        max_lines=4,
    )
    draw_lines(
        draw,
        fitted,
        left=margin,
        top=int(HEIGHT * 0.10),
        fill=pal.WHITE,
        align_center_width=content,
    )

    brand = load_font(max(16, int(HEIGHT * 0.019)), bold=True)
    draw.text(
        (margin, HEIGHT - int(HEIGHT * 0.05)),
        spec.product.name.upper(),
        font=brand,
        fill=pal.WHITE,
    )
    return frame


def _end_card(base: Image.Image, spec: CampaignSpec, cta_text: str) -> Image.Image:
    """Final frame: product still legible behind a strong CTA."""
    frame = base.convert("RGBA")
    wash = Image.new("RGBA", (WIDTH, HEIGHT), (*spec.palette.as_tuple("background"), 210))
    frame.alpha_composite(wash)
    frame = frame.convert("RGB")
    draw = ImageDraw.Draw(frame)
    margin = int(WIDTH * 0.09)
    content = WIDTH - 2 * margin
    headline = fit_text(
        draw,
        spec.headline,
        max_width=content,
        max_height=int(HEIGHT * 0.22),
        max_size=int(HEIGHT * 0.058),
        bold=True,
        max_lines=3,
    )
    y = draw_lines(
        draw,
        headline,
        left=margin,
        top=int(HEIGHT * 0.22),
        fill=pal.WHITE,
        align_center_width=content,
    )

    # CTA pill, matching the stills so the campaign reads as one system.
    label = cta_text or spec.cta_text
    pill_h = int(HEIGHT * 0.058)
    font = load_font(max(18, int(pill_h * 0.40)), bold=True)
    bbox = draw.textbbox((0, 0), label, font=font)
    text_w, text_h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pill_w = min(text_w + int(pill_h * 1.6), content)
    pill_left = (WIDTH - pill_w) // 2
    pill_top = y + int(HEIGHT * 0.045)

    accent = spec.palette.as_tuple("accent")
    on_accent = (
        pal.NEAR_BLACK
        if pal.contrast_ratio(pal.NEAR_BLACK, accent) > pal.contrast_ratio(pal.WHITE, accent)
        else pal.WHITE
    )
    draw.rounded_rectangle(
        [pill_left, pill_top, pill_left + pill_w, pill_top + pill_h],
        radius=pill_h // 2,
        fill=accent,
    )
    draw.text(
        (pill_left + (pill_w - text_w) // 2 - bbox[0],
         pill_top + (pill_h - text_h) // 2 - bbox[1]),
        label,
        font=font,
        fill=on_accent,
    )

    brand = load_font(max(18, int(HEIGHT * 0.022)), bold=True)
    bw = draw.textbbox((0, 0), spec.product.name.upper(), font=brand)
    draw.text(
        ((WIDTH - (bw[2] - bw[0])) // 2, pill_top + pill_h + int(HEIGHT * 0.035)),
        spec.product.name.upper(),
        font=brand,
        fill=pal.WHITE,
    )
    return frame


# --------------------------------------------------------------------------
# ffmpeg
# --------------------------------------------------------------------------


def build_filter_graph(durations: list[float], motions: list[str]) -> str:
    """Ken Burns per clip, crossfaded together.
    """
    parts: list[str] = []
    for index, (duration, motion) in enumerate(zip(durations, motions, strict=True)):
        frames = max(1, round(duration * FPS))
        zoom_expr, x_expr, y_expr = _motion_expressions(motion, frames)
        parts.append(
            f"[{index}:v]"
            f"scale={int(WIDTH * SUPERSAMPLE)}:{int(HEIGHT * SUPERSAMPLE)},"
            f"zoompan=z='{zoom_expr}':x='{x_expr}':y='{y_expr}':"
            f"d=1:s={WIDTH}x{HEIGHT}:fps={FPS},"
            f"setsar=1,format=yuv420p[v{index}]"
        )

    if len(durations) == 1:
        parts.append("[v0]null[vout]")
        return ";".join(parts)

    current = "v0"
    running = durations[0]
    for index in range(1, len(durations)):
        offset = round(running - TRANSITION_S, 3)
        label = "vout" if index == len(durations) - 1 else f"x{index}"
        parts.append(
            f"[{current}][v{index}]"
            f"xfade=transition=fade:duration={TRANSITION_S}:offset={offset}[{label}]"
        )
        running = running + durations[index] - TRANSITION_S
        current = label
    return ";".join(parts)


def _motion_expressions(motion: str, frames: int) -> tuple[str, str, str]:
    """Map the spec's plain-language motion onto zoompan expressions."""
    text = (motion or "").casefold()
    centre_x = "iw/2-(iw/zoom/2)"
    centre_y = "ih/2-(ih/zoom/2)"

    if "out" in text or "pull" in text:
        return ("max(1.10-0.0010*on,1.0)", centre_x, centre_y)
    if "drift" in text or "pan" in text or "across" in text:
        # Hold scale, travel horizontally across the over-scanned frame.
        return ("1.08", f"(iw-iw/zoom)*(on/{max(frames - 1, 1)})", centre_y)
    if "settle" in text or "hold" in text or "static" in text:
        return ("min(1.0+0.0004*on,1.03)", centre_x, centre_y)
    # Default: slow push in.
    return ("min(1.0+0.0009*on,1.09)", centre_x, centre_y)


async def render_video(
    *,
    frames: list[Image.Image],
    spec: CampaignSpec,
    work_dir: Path,
    output_path: Path,
    ffmpeg_bin: str,
    ffprobe_bin: str,
    timeout_s: float,
) -> ProbeResult:
    """Render the MP4 and verify it. Raises on any contract violation."""
    check_ffmpeg(ffmpeg_bin, ffprobe_bin)
    work_dir.mkdir(parents=True, exist_ok=True)

    beats = spec.video.beats
    if len(frames) != len(beats):
        raise VideoRenderError(f"{len(frames)} frames for {len(beats)} beats")

    durations = [
        round(b.seconds + (TRANSITION_S if i < len(beats) - 1 else 0.0), 3)
        for i, b in enumerate(beats)
    ]

    frame_paths: list[Path] = []
    for index, frame in enumerate(frames):
        path = work_dir / f"beat_{index}.png"
        frame.save(path, format="PNG")
        frame_paths.append(path)

    args: list[str] = [ffmpeg_bin, "-y", "-hide_banner", "-loglevel", "error"]
    for duration, path in zip(durations, frame_paths, strict=True):
        args += ["-loop", "1", "-framerate", str(FPS), "-t", f"{duration}", "-i", str(path)]

    args += [
        "-filter_complex",
        build_filter_graph(durations, [b.motion for b in beats]),
        "-map", "[vout]",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-r", str(FPS),
        "-movflags", "+faststart",
        str(output_path),
    ]

    log.info("video.render_start", beats=len(beats), target_s=spec.video.total_seconds)
    await _run(args, timeout_s=timeout_s, label="ffmpeg")

    probe = await probe_video(output_path, ffprobe_bin=ffprobe_bin, timeout_s=30)

    # Assert the contract rather than trusting it.
    if (probe.width, probe.height) != (WIDTH, HEIGHT):
        raise VideoRenderError(
            f"rendered {probe.width}x{probe.height}, required {WIDTH}x{HEIGHT}",
            kind="video_dimensions_wrong",
        )
    if not 6.0 <= probe.duration_s <= 10.0:
        raise VideoRenderError(
            f"rendered duration {probe.duration_s:.2f}s outside the required 6-10s",
            kind="video_duration_wrong",
        )

    log.info(
        "video.render_ok",
        duration_s=round(probe.duration_s, 2),
        size=f"{probe.width}x{probe.height}",
        codec=probe.codec,
    )
    return probe


async def probe_video(path: Path, *, ffprobe_bin: str, timeout_s: float = 30) -> ProbeResult:
    stdout = await _run(
        [
            ffprobe_bin, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,codec_name:format=duration",
            "-of", "json",
            str(path),
        ],
        timeout_s=timeout_s,
        label="ffprobe",
    )
    payload = json.loads(stdout or "{}")
    streams = payload.get("streams") or []
    if not streams:
        raise VideoRenderError(f"ffprobe found no video stream in {path.name}")
    stream = streams[0]
    duration = float((payload.get("format") or {}).get("duration") or 0.0)
    return ProbeResult(
        width=int(stream["width"]),
        height=int(stream["height"]),
        duration_s=duration,
        codec=str(stream.get("codec_name", "")),
    )


async def _run(args: list[str], *, timeout_s: float, label: str) -> str:
    try:
        process = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
    except FileNotFoundError as exc:
        raise FFmpegMissingError(f"{label} could not be executed: {exc}") from exc

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_s)
    except TimeoutError:
        # Kill rather than leave an orphaned encoder holding the output file.
        process.kill()
        await process.wait()
        raise VideoRenderError(
            f"{label} exceeded the {timeout_s:.0f}s render timeout and was terminated",
            kind="timeout",
        ) from None

    if process.returncode != 0:
        detail = (stderr or b"").decode("utf-8", "replace").strip()[-1200:]
        raise VideoRenderError(f"{label} exited {process.returncode}: {detail}")
    return (stdout or b"").decode("utf-8", "replace")
