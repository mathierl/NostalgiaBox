"""Poster thumbnails for the browser-based admin UI.

A poster is one representative frame grabbed from each channel's first
episode via ffmpeg. These used to be composed into a single pre-baked
background image for mpv's ASS-overlay grid (see git history before
UKE-29's Chromium pivot) - now the browser-based admin UI (admin_server.py,
admin_ui/index.html) fetches each poster directly as its own image and lays
out the grid itself in real CSS, so all that's needed here is generating and
caching the individual poster files.

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
from typing import Dict, List, Optional, Sequence

from .channel import Channel
from .probe import DEFAULT_EPISODE_SECONDS, probe_duration

log = logging.getLogger(__name__)

THUMBS_SUBDIR = "thumbnails"


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "show"


def poster_filename(channel: Channel) -> str:
    """The on-disk (and, for the browser admin UI, URL) name a channel's
    poster is cached under - the one place this naming scheme is defined,
    shared by ensure_channel_poster below and TVApp's admin state snapshot
    (app.py) so a poster URL the browser is given always matches a real
    file on disk.
    """
    return f"channel-{channel.number:02d}-{_slugify(channel.name)}.jpg"


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
    from - the browser admin UI falls back to a plain placeholder tile for
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


def generate_admin_assets(
    tiles: Sequence[Channel], cache_dir: Path, *, force: bool = False
) -> Dict[int, Path]:
    """Top-level entry point used by ``--check``: (re)generate any missing/
    stale channel posters. Best-effort throughout - a missing ffmpeg/Pillow
    never breaks config validation, it just means fewer (or no) posters are
    available and the browser admin UI falls back to placeholder tiles.
    ``tiles`` is real channels and game systems combined (see
    ``nostalgiabox.app.TVApp._admin_tiles``). Returns the posters that were
    generated or already cached, keyed by channel number.
    """
    return ensure_all_posters(tiles, cache_dir, force=force)


__all__ = [
    "THUMBS_SUBDIR",
    "ffmpeg_available",
    "poster_filename",
    "extract_poster",
    "ensure_channel_poster",
    "ensure_all_posters",
    "generate_admin_assets",
]
