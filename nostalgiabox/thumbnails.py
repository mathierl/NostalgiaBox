"""Poster thumbnails for the admin/developer view's show grid.

Unlike the retro CRT overlays (pure ASS text/vector drawing), the modern
admin-mode show grid uses real poster art - a frame grabbed from each
channel's first episode via ffmpeg. Since mpv's ``osd-overlay`` (ass-events)
mechanism can only draw text and vector shapes, not raster images, real
posters can't be layered on top of the live video the way the channel banner
or volume bar are. Instead, the whole grid (dark background + every poster,
positioned to line up with where :mod:`nostalgiabox.overlay` draws the
highlight ring and labels) is composed once into a single image with Pillow,
and displayed by swapping the player onto it (:meth:`Player.play_loop`,
the same mechanism already used for the static/colour-bars filler clips) -
see :class:`nostalgiabox.app.TVApp` for how playback is remembered and
resumed around this.

Generation only ever happens explicitly - during ``nostalgiabox --check``
(and so also during ``scripts/install.sh``) - never on the fly while
browsing, so opening admin mode on the Pi is always instant and never
competes with live playback for CPU. Per-channel posters are cached to disk
and keyed off the source episode's modification time, so re-running
``--check`` after adding new shows only (re)generates what's actually new.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from pathlib import Path
from typing import Dict, List, Optional

from .channel import Channel, ChannelLineup
from .probe import DEFAULT_EPISODE_SECONDS, probe_duration

log = logging.getLogger(__name__)

THUMBS_SUBDIR = "thumbnails"
GRID_FILENAME = "show_grid.jpg"

# The grid is composed at the same resolution the video ends up at after the
# app's force_4_3 filter (see player.MpvPlayer) - 960x720 - so it passes
# through that filter chain unchanged when played.
_GRID_W = 960
_GRID_H = 720

# Layout of the poster grid - kept in sync with the highlight-ring/label
# positions overlay.py draws on top, via admin_grid_tile_rect() below.
GRID_COLS = 2
_GRID_MARGIN_X = 56
_GRID_HEADER_H = 96
_GRID_FOOTER_H = 56
_GRID_GAP = 28
_GRID_LABEL_H = 60
_POSTER_ASPECT = 16 / 9

_BG_COLOR = (0x14, 0x14, 0x14)
_PLACEHOLDER_COLOR = (0x2A, 0x2A, 0x2A)


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def pillow_available() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "show"


def admin_grid_tile_rect(index: int, count: int) -> tuple[int, int, int, int]:
    """Pixel rect ``(x, y, w, h)`` for poster ``index`` of ``count``, in the
    same local (0,0)-(960,720) coordinate space the composed grid image is
    drawn in. Also used by overlay.py to line up the highlight ring/labels.
    """
    cols = GRID_COLS if count > 1 else 1
    rows = max(1, -(-count // cols))  # ceil division
    col_w = (_GRID_W - _GRID_MARGIN_X * 2 - _GRID_GAP * (cols - 1)) // cols
    tile_h = int(col_w / _POSTER_ASPECT)
    available_h = _GRID_H - _GRID_HEADER_H - _GRID_FOOTER_H
    row_pitch = tile_h + _GRID_LABEL_H + _GRID_GAP
    block_h = rows * row_pitch - _GRID_GAP
    start_y = _GRID_HEADER_H + max(0, (available_h - block_h) // 2)

    row, col = divmod(index, cols)
    x = _GRID_MARGIN_X + col * (col_w + _GRID_GAP)
    y = start_y + row * row_pitch
    return x, y, col_w, tile_h


def _run(cmd: List[str]) -> None:
    log.info("running: %s", " ".join(cmd))
    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=30)


def extract_poster(video_path: Path, out_path: Path, *, width: int, height: int) -> Path:
    """Grab one representative frame from ``video_path`` via ffmpeg, cropped
    to fill ``width``x``height`` (like CSS ``object-fit: cover``)."""
    duration = probe_duration(video_path) or DEFAULT_EPISODE_SECONDS
    # A little way in, never right at the very start (titles/black frames)
    # or right at the end.
    timestamp = 0.0 if duration <= 3 else max(2.0, min(duration * 0.15, duration - 1.0))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{timestamp:.2f}",
        "-i", str(video_path),
        "-frames:v", "1",
        "-vf", f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
        str(out_path),
    ]
    _run(cmd)
    return out_path


def ensure_channel_poster(
    channel: Channel, cache_dir: Path, *, force: bool = False
) -> Optional[Path]:
    """Return a cached poster for ``channel``, (re)generating it if the
    source episode is newer than the cached poster (or there isn't one yet).
    Returns None if the channel has no episodes or ffmpeg is unavailable.
    """
    if channel.is_empty:
        return None
    source = channel.episodes[0]
    out_path = cache_dir / f"channel-{channel.number:02d}-{_slugify(channel.name)}.jpg"

    if not force and out_path.exists():
        try:
            if out_path.stat().st_mtime >= source.stat().st_mtime:
                return out_path
        except OSError:
            pass

    if not ffmpeg_available():
        log.warning("ffmpeg not available; cannot generate poster for %s", channel.name)
        return out_path if out_path.exists() else None

    try:
        return extract_poster(source, out_path, width=640, height=360)
    except (subprocess.SubprocessError, OSError) as exc:
        log.warning(
            "could not generate poster for channel %s (%s): %s",
            channel.number, channel.name, exc,
        )
        return out_path if out_path.exists() else None


def ensure_all_posters(
    lineup: ChannelLineup, cache_dir: Path, *, force: bool = False
) -> Dict[int, Path]:
    posters: Dict[int, Path] = {}
    for channel in lineup:
        poster = ensure_channel_poster(channel, cache_dir, force=force)
        if poster is not None:
            posters[channel.number] = poster
    return posters


def _cover_resize(img, width: int, height: int):
    from PIL import Image

    src_w, src_h = img.size
    scale = max(width / src_w, height / src_h)
    new_w, new_h = max(1, round(src_w * scale)), max(1, round(src_h * scale))
    img = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - width) // 2
    top = (new_h - height) // 2
    return img.crop((left, top, left + width, top + height))


def compose_show_grid(
    lineup: ChannelLineup, posters: Dict[int, Path], out_path: Path
) -> Optional[Path]:
    """Render the full show-grid background image: dark backdrop plus every
    channel's poster (or a plain placeholder tile if it has none), positioned
    via :func:`admin_grid_tile_rect` so overlay.py's highlight ring and text
    line up with it exactly.
    """
    if not pillow_available():
        log.warning("Pillow not available; cannot compose the admin show grid")
        return None
    from PIL import Image

    channels = list(lineup)
    bg = Image.new("RGB", (_GRID_W, _GRID_H), _BG_COLOR)
    for i, channel in enumerate(channels):
        x, y, w, h = admin_grid_tile_rect(i, len(channels))
        poster_path = posters.get(channel.number)
        tile = None
        if poster_path is not None and poster_path.exists():
            try:
                with Image.open(poster_path) as src:
                    tile = _cover_resize(src.convert("RGB"), w, h)
            except OSError:
                tile = None
        if tile is None:
            tile = Image.new("RGB", (w, h), _PLACEHOLDER_COLOR)
        bg.paste(tile, (x, y))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    bg.save(out_path, quality=88)
    return out_path


def generate_admin_assets(
    lineup: ChannelLineup, cache_dir: Path, *, force: bool = False
) -> Optional[Path]:
    """Top-level entry point used by ``--check``: (re)generate any missing/
    stale channel posters, then compose the full show-grid background.
    Best-effort throughout - returns None (and logs a warning) rather than
    raising, so a missing ffmpeg/Pillow never breaks config validation.
    """
    posters = ensure_all_posters(lineup, cache_dir, force=force)
    return compose_show_grid(lineup, posters, cache_dir / GRID_FILENAME)


__all__ = [
    "THUMBS_SUBDIR",
    "GRID_FILENAME",
    "GRID_COLS",
    "ffmpeg_available",
    "pillow_available",
    "admin_grid_tile_rect",
    "extract_poster",
    "ensure_channel_poster",
    "ensure_all_posters",
    "compose_show_grid",
    "generate_admin_assets",
]
