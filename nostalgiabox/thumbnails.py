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

Note (UKE-29): this module briefly grew a second life generating individual
poster files for a browser-based (Chromium/cage) admin UI. That approach was
reverted - closing mpv to hand the display to a second process racing it for
DRM ownership turned out to reliably segfault the whole box on real
hardware. Back to the single mpv-owned image this module was built for
originally; see git history around "Replace ASS admin-mode grid/episode-list
with a real Chromium-kiosk browser UI" / "Revert to native mpv rendering" if
you need the browser version for reference.

Two more UKE-29 follow-ups, after real-hardware feedback on the reverted
mpv+ASS UI:

* The grid is composed at the *full* 16:9 canvas (:data:`GRID_W` x
  :data:`GRID_H`, matching :data:`nostalgiabox.overlay.CANVAS_W`/``CANVAS_H``)
  rather than the 4:3 frame every actual show plays in. Real shows/filler
  clips stay pillarboxed (that's the point of the nostalgic 4:3 "tube"), but
  the admin UI is deliberately a *modern* Netflix-ish screen (see
  overlay.py's module docstring) - confining it to the same 960px-wide inset
  just wasted 25% of the screen for no reason. :class:`nostalgiabox.player.
  MpvPlayer` bypasses the 4:3 filter for this one image via
  ``play_loop(..., use_frame_filter=False)``.
* A section (Shows/Games) no longer shrinks its tiles to cram every one into
  a single row - tiles stay a fixed, comfortable size and *wrap* onto
  additional rows within the section instead (:func:`admin_section_layout`).
  Combined with the always-present Continue Watching/Insights/Adult-Mode
  rows, that means the whole screen can now be taller than one 720px canvas
  - :func:`sections_bottom` is the shared height calculation
  :mod:`nostalgiabox.overlay` uses to know how far down the evergreen rows
  sit, and :func:`crop_viewport` is how the app scrolls: the *whole* grid
  (however tall) is still composed once at ``--check`` time as before, and
  a cheap runtime crop (no ffmpeg, no poster work - just already-decoded
  pixels) produces the single screen's worth mpv actually displays.
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
# Runtime-generated crop of GRID_FILENAME for whatever's currently scrolled
# into view (see crop_viewport) - unlike GRID_FILENAME this is NOT meant to
# survive a reboot or be checked into anything; it's regenerated on the fly
# as the browse cursor moves between sections.
VIEWPORT_FILENAME = "show_grid_viewport.jpg"

# The grid is composed at the full 16:9 canvas size - matching
# nostalgiabox.overlay.CANVAS_W/CANVAS_H exactly, which is what makes the
# admin UI fill the whole screen edge to edge instead of being confined to
# the 4:3 "tube" every actual show plays in (see the module docstring).
GRID_W = 1280
GRID_H = 720

# Layout of the poster grid - kept in sync with the section-label/highlight-
# ring positions overlay.py draws on top, via admin_section_layout() below.
# Real channels and game systems are grouped into named "Shows"/"Games"
# swimlanes (UKE-29) - each may wrap onto multiple rows within its section
# if it has more tiles than comfortably fit at a fixed size on one line (see
# admin_section_layout) - so the composed image can end up taller than one
# screen, in which case the app scrolls it (see crop_viewport). This is
# still safe to bake into the once-per---check background image, unlike the
# Continue Watching row (see overlay.py): which shows/games exist only
# changes when the config does, not on every watch.
_GRID_MARGIN_X = 56
# Reserves room for the "Select a channel" header line *and*, below it, the
# "Continue Watching" text row overlay.py draws (see CONTINUE_LIMIT there).
# This is baked into the pre-generated background image (see module
# docstring), so the space is always reserved even on a run with nothing
# in progress - there's no way to know that at --check time, only at browse
# time. A public constant (not a leading underscore) since overlay.py needs
# it to position the continue-watching text at the same header height. This
# header (and the Continue Watching row) never scrolls - see crop_viewport.
GRID_HEADER_H = 188
GRID_FOOTER_H = 56
_SECTION_LABEL_H = 22
_SECTION_LABEL_GAP = 8
_SECTION_ROW_GAP = 12
_GRID_LABEL_H = 58  # room below each tile for its title/subtitle text
_TILE_GAP = 24
_TILE_ROW_GAP = 12  # vertical gap between a section's *wrapped* rows
_TILE_MAX_W = 260
_POSTER_ASPECT = 16 / 9

# Height of the two evergreen rows overlay.py draws below the last real
# section - Watch Insights and the Adult Mode toggle (UKE-29) - neither of
# which is baked into the background image (like Continue Watching, they're
# not something --check can know ahead of time... well, Adult Mode's on/off
# state at least changes live). A shared constant so thumbnails.py's height
# accounting and overlay.py's row drawing can never drift apart - see
# scrollable_content_height.
EVERGREEN_ROW_H = 64
EVERGREEN_GAP_ABOVE = 20

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


def poster_filename(channel: Channel) -> str:
    """The on-disk name a channel's poster is cached under - the one place
    this naming scheme is defined.
    """
    return f"channel-{channel.number:02d}-{_slugify(channel.name)}.jpg"


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
    """Pixel layout - in the same local (0,0)-(GRID_W,*) coordinate space the
    composed grid image is drawn in - for every section's label and tiles.
    The one shared source of truth between the background image
    (:func:`compose_show_grid`) and overlay.py's section-label/highlight-
    ring drawing, so they can never drift apart.

    Each section is one or more horizontal rows at a fixed, comfortable tile
    size (``_TILE_MAX_W``) - a section with more tiles than fit on one line
    *wraps* onto additional rows within itself (UKE-29) rather than
    shrinking every tile to cram them all into a single row, so posters
    never get tiny just because a show list is long. The vertical result can
    end up taller than one screen; see crop_viewport for how that's scrolled.
    """
    usable_w = GRID_W - _GRID_MARGIN_X * 2
    tile_w = min(_TILE_MAX_W, usable_w)
    tile_h = int(tile_w / _POSTER_ASPECT)
    cols = max(1, (usable_w + _TILE_GAP) // (tile_w + _TILE_GAP))
    y = GRID_HEADER_H
    sections: List[Section] = []
    for title, channels in admin_sections(tiles):
        label_y = y
        row_y = label_y + _SECTION_LABEL_H + _SECTION_LABEL_GAP
        section_tiles: List[SectionTile] = []
        cur_y = row_y
        for i, channel in enumerate(channels):
            col = i % cols
            if i > 0 and col == 0:
                cur_y += tile_h + _GRID_LABEL_H + _TILE_ROW_GAP
            x = _GRID_MARGIN_X + col * (tile_w + _TILE_GAP)
            section_tiles.append(SectionTile(channel=channel, x=x, y=cur_y, w=tile_w, h=tile_h))
        sections.append(Section(title=title, label_y=label_y, tiles=section_tiles))
        y = cur_y + tile_h + _GRID_LABEL_H + _SECTION_ROW_GAP
    return sections


def tile_bounds(tile: SectionTile) -> Tuple[int, int]:
    """(top, bottom) image-space Y-extent of one tile, *including* the title/
    subtitle text drawn below its poster (see _GRID_LABEL_H) - used by
    nostalgiabox.app.TVApp._sync_admin_scroll to keep not just the poster
    but its label visible too when the cursor moves onto it within a
    wrapped section (see admin_section_layout).
    """
    return tile.y, tile.y + tile.h + _GRID_LABEL_H


def section_bounds(tiles: Sequence[Channel], title: str) -> Optional[Tuple[int, int]]:
    """(top, bottom) image-space Y-extent of one named section ("Shows" or
    "Games") - used by nostalgiabox.app.TVApp._sync_admin_scroll to know
    what to scroll into view when the browse cursor moves onto that row.
    ``None`` if there's no such section (e.g. no games configured at all).
    """
    for section in admin_section_layout(list(tiles)):
        if section.title == title and section.tiles:
            bottom = max(tile.y + tile.h + _GRID_LABEL_H for tile in section.tiles)
            return section.label_y, bottom
    return None


def sections_bottom(tiles: Sequence[Channel]) -> int:
    """Y coordinate just below the last section's last row - i.e. where the
    evergreen Insights/Adult-Mode rows (drawn by overlay.py, never baked
    into the background image) start. ``GRID_HEADER_H`` if there are no
    sections at all (no shows or games configured).
    """
    bottom = GRID_HEADER_H
    for section in admin_section_layout(list(tiles)):
        for tile in section.tiles:
            bottom = max(bottom, tile.y + tile.h + _GRID_LABEL_H)
    return bottom


def scrollable_content_height(tiles: Sequence[Channel], *, evergreen_rows: int = 2) -> int:
    """Total height of the scrollable part of the browse screen: every
    section's tiles, plus ``evergreen_rows`` fixed-height rows below them
    (Watch Insights and the Adult Mode toggle, UKE-29 - see EVERGREEN_ROW_H)
    and a little footer breathing room. This - not just ``sections_bottom``
    - is what the composed background image needs to be tall enough to
    cover (see compose_show_grid) and what the app clamps scrolling against
    (see crop_viewport).
    """
    bottom = sections_bottom(tiles)
    bottom += evergreen_rows * (EVERGREEN_ROW_H + EVERGREEN_GAP_ABOVE)
    return bottom + GRID_FOOTER_H


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
    out_path = cache_dir / poster_filename(channel)

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

    The image is exactly ``GRID_W`` wide but may be *taller* than ``GRID_H``
    when there's more content than fits one screen (see
    :func:`scrollable_content_height`) - the app crops a screen's worth out
    of it at browse time (see :func:`crop_viewport`) rather than this
    function ever needing to know about scrolling itself.
    """
    if not pillow_available():
        log.warning("Pillow not available; cannot compose the admin show grid")
        return None
    from PIL import Image

    height = max(GRID_H, scrollable_content_height(tiles))
    bg = Image.new("RGB", (GRID_W, height), _BG_COLOR)
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


def clamp_scroll(tiles: Sequence[Channel], scroll_y: int) -> int:
    """Clamp a candidate vertical scroll offset to ``[0, max]`` for this set
    of tiles - the one shared bound :class:`nostalgiabox.app.TVApp` (picking
    a target to scroll to) and :func:`crop_viewport` (actually cropping)
    both need, so they can't disagree about how far scrolling is allowed to go.
    """
    max_scroll = max(0, scrollable_content_height(tiles) - GRID_H)
    return max(0, min(int(scroll_y), max_scroll))


def body_viewport_height() -> int:
    """Height of the scrollable area between the pinned header (see
    GRID_HEADER_H) and the footer-hint text's clearance (see
    GRID_FOOTER_H) - i.e. how tall a "page" of the admin grid actually is
    once you subtract the chrome that never scrolls. Used by
    nostalgiabox.app.TVApp._sync_admin_scroll to decide whether a given row
    is already fully visible before bothering to scroll to it.
    """
    return GRID_H - GRID_HEADER_H - GRID_FOOTER_H


def crop_viewport(
    image_path: Path,
    scroll_y: int,
    out_path: Path,
    *,
    width: int = GRID_W,
    height: int = GRID_H,
    header_h: int = GRID_HEADER_H,
) -> Optional[Path]:
    """Crop a ``width``x``height`` screen's worth out of the (possibly
    taller) composed background image at vertical offset ``scroll_y`` -
    admin-grid scrolling (UKE-29), driven live as the browse cursor moves
    between sections (see ``nostalgiabox.app.TVApp._sync_admin_scroll`` /
    ``crop_viewport``'s use in ``_show_admin_grid_background``).

    The top ``header_h`` rows are always the source image's own top rows,
    *unscrolled* - they're flat background colour with nothing baked there
    (see compose_show_grid; the header/Continue-Watching text overlay.py
    draws over them is likewise pinned, at a fixed canvas position never
    adjusted by scroll_y), so this keeps them stable while only the rows
    below scroll. Everything from ``header_h`` down comes from the source at
    ``header_h + scroll_y``, clamped (see :func:`clamp_scroll`) so it never
    runs past either end of the source image.

    Unlike :func:`compose_show_grid` (ffmpeg poster extraction + a once-per-
    ``--check`` composite), this is *cheap* - a crop and JPEG re-encode of
    pixels that are already fully rendered - so it's safe to call on every
    scroll change without the "never compete with live playback" concern
    the rest of this module's docstring describes (browsing already isn't
    live playback: the grid image is what's showing *instead* of video).
    """
    if not pillow_available():
        return None
    from PIL import Image

    try:
        with Image.open(image_path) as img:
            img_w, img_h = img.size
            body_h = height - header_h
            max_scroll = max(0, img_h - header_h - body_h)
            top = max(0, min(int(scroll_y), max_scroll))

            canvas = Image.new("RGB", (width, height), _BG_COLOR)
            header_box = (0, 0, min(width, img_w), min(header_h, img_h))
            canvas.paste(img.crop(header_box), (0, 0))
            body_box = (
                0,
                header_h + top,
                min(width, img_w),
                min(header_h + top + body_h, img_h),
            )
            canvas.paste(img.crop(body_box), (0, header_h))
            canvas.load()
    except OSError:
        log.warning("could not crop admin grid viewport from %s", image_path)
        return None

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, quality=88)
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
    "VIEWPORT_FILENAME",
    "GRID_W",
    "GRID_H",
    "GRID_HEADER_H",
    "GRID_FOOTER_H",
    "EVERGREEN_ROW_H",
    "EVERGREEN_GAP_ABOVE",
    "Section",
    "SectionTile",
    "ffmpeg_available",
    "pillow_available",
    "poster_filename",
    "admin_sections",
    "admin_section_layout",
    "tile_bounds",
    "section_bounds",
    "sections_bottom",
    "scrollable_content_height",
    "clamp_scroll",
    "body_viewport_height",
    "extract_poster",
    "ensure_channel_poster",
    "ensure_all_posters",
    "compose_show_grid",
    "crop_viewport",
    "generate_admin_assets",
]
