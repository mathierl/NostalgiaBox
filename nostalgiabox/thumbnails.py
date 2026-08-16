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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .channel import Channel
from .probe import DEFAULT_EPISODE_SECONDS, probe_duration

log = logging.getLogger(__name__)

THUMBS_SUBDIR = "thumbnails"
GRID_FILENAME = "show_grid.jpg"

# The grid is composed at the same resolution the video ends up at after the
# app's force_4_3 filter (see player.MpvPlayer) - 960x720 - so it passes
# through that filter chain unchanged when played.
_GRID_W = 960
_GRID_H = 720

# Layout of the poster grid - kept in sync with the section-label/highlight-
# ring positions overlay.py draws on top, via admin_section_layout() below.
# Real channels and game systems are grouped into named "Shows"/"Games"
# swimlanes (UKE-29) - each its own single horizontal row, sized to fit
# however many tiles it holds (no scrolling/pagination - the same tradeoff
# the single flat grid this replaces already made). This is safe to bake
# into the once-per---check background image, unlike the Continue Watching
# row (see overlay.py): which shows/games exist only changes when the
# config does, not on every watch.
_GRID_MARGIN_X = 56
# Reserves room for the "Select a channel" header line *and*, below it, the
# "Continue Watching" text row overlay.py draws (see CONTINUE_LIMIT there).
# This is baked into the pre-generated background image (see module
# docstring), so the space is always reserved even on a run with nothing
# in progress - there's no way to know that at --check time, only at browse
# time. A public constant (not a leading underscore) since overlay.py needs
# it to position the continue-watching text at the same header height.
GRID_HEADER_H = 188
_GRID_FOOTER_H = 56
_SECTION_LABEL_H = 22
_SECTION_LABEL_GAP = 8
_SECTION_ROW_GAP = 12
_GRID_LABEL_H = 58  # room below each tile for its title/subtitle text
_TILE_GAP = 24
_TILE_MAX_W = 240
_POSTER_ASPECT = 16 / 9

_BG_COLOR = (0x14, 0x14, 0x14)
_PLACEHOLDER_COLOR = (0x2A, 0x2A, 0x2A)


@dataclass(frozen=True)
class SectionTile:
    """One poster's pixel rect within a section's row."""

    channel: Channel
    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class Section:
    """One named swimlane ("Shows", "Games") and its laid-out tiles."""

    title: str
    label_y: int
    tiles: List[SectionTile] = field(default_factory=list)


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


def admin_sections(tiles: Sequence[Channel]) -> List[Tuple[str, List[Channel]]]:
    """Split combined admin tiles (real channels + game systems, in
    :meth:`nostalgiabox.app.TVApp._admin_tiles` order) into the named
    swimlanes the admin browse screen groups them into - "Shows" then
    "Games", each only present if it actually has anything in it. A
    channel's ``config.kind`` is the only thing that decides which lane
    it's in.
    """
    shows = [c for c in tiles if c.config.kind != "game"]
    games = [c for c in tiles if c.config.kind == "game"]
    sections: List[Tuple[str, List[Channel]]] = []
    if shows:
        sections.append(("Shows", shows))
    if games:
        sections.append(("Games", games))
    return sections


def admin_section_layout(tiles: Sequence[Channel]) -> List[Section]:
    """Pixel layout - in the same local (0,0)-(960,720) coordinate space the
    composed grid image is drawn in - for every section's label and tiles.
    The one shared source of truth between the background image
    (:func:`compose_show_grid`) and overlay.py's section-label/highlight-
    ring drawing, so they can never drift apart.

    Each section is one horizontal row, tiles sized to fit however many it
    holds (capped at ``_TILE_MAX_W`` so a lone show/system doesn't get a
    giant poster) - no scrolling or pagination, the same tradeoff the
    single flat grid this replaces already made.
    """
    usable_w = _GRID_W - _GRID_MARGIN_X * 2
    y = GRID_HEADER_H
    sections: List[Section] = []
    for title, channels in admin_sections(tiles):
        label_y = y
        row_y = label_y + _SECTION_LABEL_H + _SECTION_LABEL_GAP
        count = len(channels)
        tile_w = min(_TILE_MAX_W, (usable_w - _TILE_GAP * (count - 1)) // count)
        tile_h = int(tile_w / _POSTER_ASPECT)
        x = _GRID_MARGIN_X
        section_tiles: List[SectionTile] = []
        for channel in channels:
            section_tiles.append(SectionTile(channel=channel, x=x, y=row_y, w=tile_w, h=tile_h))
            x += tile_w + _TILE_GAP
        sections.append(Section(title=title, label_y=label_y, tiles=section_tiles))
        y = row_y + tile_h + _GRID_LABEL_H + _SECTION_ROW_GAP
    return sections


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
    Returns None if the channel has no episodes, ffmpeg is unavailable, or
    it's a game channel (a ROM file isn't something ffmpeg can grab a frame
    from - compose_show_grid falls back to a plain placeholder tile for
    these, same as it does for any channel with no poster).
    """
    if channel.is_empty or channel.config.kind == "game":
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
    tiles: Sequence[Channel], cache_dir: Path, *, force: bool = False
) -> Dict[int, Path]:
    posters: Dict[int, Path] = {}
    for channel in tiles:
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
    tiles: Sequence[Channel], posters: Dict[int, Path], out_path: Path
) -> Optional[Path]:
    """Render the full show-grid background image: dark backdrop plus every
    channel's poster (or a plain placeholder tile if it has none - this is
    what every game-system tile gets, since a ROM has no frame to grab),
    positioned via :func:`admin_section_layout` so overlay.py's section
    labels and highlight ring line up with it exactly. ``tiles`` is real
    channels and game systems combined, in the order they should appear.
    """
    if not pillow_available():
        log.warning("Pillow not available; cannot compose the admin show grid")
        return None
    from PIL import Image

    bg = Image.new("RGB", (_GRID_W, _GRID_H), _BG_COLOR)
    for section in admin_section_layout(list(tiles)):
        for tile in section.tiles:
            poster_path = posters.get(tile.channel.number)
            img = None
            if poster_path is not None and poster_path.exists():
                try:
                    with Image.open(poster_path) as src:
                        img = _cover_resize(src.convert("RGB"), tile.w, tile.h)
                except OSError:
                    img = None
            if img is None:
                img = Image.new("RGB", (tile.w, tile.h), _PLACEHOLDER_COLOR)
            bg.paste(img, (tile.x, tile.y))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    bg.save(out_path, quality=88)
    return out_path


def generate_admin_assets(
    tiles: Sequence[Channel], cache_dir: Path, *, force: bool = False
) -> Optional[Path]:
    """Top-level entry point used by ``--check``: (re)generate any missing/
    stale channel posters, then compose the full show-grid background.
    Best-effort throughout - returns None (and logs a warning) rather than
    raising, so a missing ffmpeg/Pillow never breaks config validation.
    ``tiles`` is real channels and game systems combined (see
    ``nostalgiabox.app.TVApp._admin_tiles``).
    """
    posters = ensure_all_posters(tiles, cache_dir, force=force)
    return compose_show_grid(tiles, posters, cache_dir / GRID_FILENAME)


__all__ = [
    "THUMBS_SUBDIR",
    "GRID_FILENAME",
    "GRID_HEADER_H",
    "Section",
    "SectionTile",
    "ffmpeg_available",
    "pillow_available",
    "admin_sections",
    "admin_section_layout",
    "extract_poster",
    "ensure_channel_poster",
    "ensure_all_posters",
    "compose_show_grid",
    "generate_admin_assets",
]
