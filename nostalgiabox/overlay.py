"""On-screen display: the green digital channel banner, volume bar, and messages.

These are drawn to look like a late-90s/early-2000s TV's on-screen display: a
chunky phosphor-green readout in a retro terminal font, with a soft CRT glow.
Two signature elements:

* the **channel banner** ("CH 03" + the show name) that flashes top-right when
  you change channels, and
* the **volume bar** - a row of solid green bars for the current level followed
  by green dots for the rest, with a "Volume" label - matching a classic TV OSD.

Everything is rendered as ASS overlays on a fixed 1280x720 virtual canvas (mpv
scales it to the TV) and cleared automatically after a few seconds by
:meth:`OverlayManager.tick`, which the main loop calls every iteration.
"""

from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional, Sequence

from .channel import Channel, ChannelLineup, episode_title
from .config import Config, UiConfig
from .player import Player
from .thumbnails import admin_section_layout
from .watch_state import ContinueEntry

# Virtual canvas the overlays are laid out on. This maps to the WHOLE display
# (a 16:9 TV), so mpv scales it to whatever the screen is.
CANVAS_W = 1280
CANVAS_H = 720

# The video is forced into a 4:3 frame centred on the 16:9 canvas (see
# MpvPlayer.force_4_3). We lay the OSD out *inside* that 4:3 frame - with a small
# safe-area inset so nothing sits under the CRT's rounded corners - so the green
# readouts always sit over the picture, never out in the black pillarbox bars.
_FRAME_W = int(round(CANVAS_H * 4 / 3))        # 960
_FRAME_X0 = (CANVAS_W - _FRAME_W) // 2          # 160
_FRAME_X1 = _FRAME_X0 + _FRAME_W                # 1120
_FRAME_CX = (_FRAME_X0 + _FRAME_X1) // 2        # 640
_SAFE = 0.06
_IX0 = _FRAME_X0 + int(_FRAME_W * _SAFE)        # ~217  (left safe edge)
_IX1 = _FRAME_X1 - int(_FRAME_W * _SAFE)        # ~1062 (right safe edge)
_IY0 = int(CANVAS_H * _SAFE)                     # ~43   (top safe edge)
_IY1 = CANVAS_H - int(CANVAS_H * _SAFE)          # ~677  (bottom safe edge)

# Overlay slots (ids). Each kind of overlay owns one id so it can be replaced
# or cleared independently.
_ID_CHANNEL = 1
_ID_VOLUME = 2
_ID_STANDBY = 3
_ID_MESSAGE = 4
_ID_ADMIN = 5

_BLACK = "&H00000000"


class OverlayManager:
    """Draws and expires the TV's on-screen overlays."""

    def __init__(
        self,
        player: Player,
        config: Config,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._player = player
        self._config = config
        self._ui = config.ui
        self._clock = clock
        # overlay id -> wall time (monotonic) at which it should disappear.
        self._expiry: Dict[int, float] = {}

    # -- public API ---------------------------------------------------------
    def show_channel_bug(
        self, number: int, name: str, *, duration: Optional[float] = None
    ) -> None:
        """Flash the channel number + name, like changing channels on a cable box."""
        dur = self._config.channel_bug_seconds if duration is None else duration
        ass = _channel_bug_ass(number, name, self._ui)
        self._player.set_overlay(_ID_CHANNEL, ass, CANVAS_W, CANVAS_H)
        self._arm(_ID_CHANNEL, dur)

    def show_volume(
        self, level: int, muted: bool, *, duration: Optional[float] = None
    ) -> None:
        dur = self._config.osd_duration if duration is None else duration
        ass = _volume_ass(level, muted, self._ui)
        self._player.set_overlay(_ID_VOLUME, ass, CANVAS_W, CANVAS_H)
        self._arm(_ID_VOLUME, dur)

    def show_message(self, text: str, *, duration: Optional[float] = None) -> None:
        dur = self._config.osd_duration if duration is None else duration
        ass = _message_ass(text, self._ui)
        self._player.set_overlay(_ID_MESSAGE, ass, CANVAS_W, CANVAS_H)
        self._arm(_ID_MESSAGE, dur)

    def show_admin_panel(self, lineup: "ChannelLineup", *, paused: bool) -> None:
        """Persistent grown-ups-only overlay: every channel, its episode
        count, and the current pause state. Reached by holding Power; see
        :class:`nostalgiabox.app.TVApp`.
        """
        ass = _admin_panel_ass(lineup, paused, self._ui)
        self._player.set_overlay(_ID_ADMIN, ass, CANVAS_W, CANVAS_H)
        self._expiry.pop(_ID_ADMIN, None)  # persistent until cleared

    def show_admin_browser(
        self,
        tiles: Sequence["Channel"],
        *,
        highlight_number: Optional[int],
        continue_entries: Sequence["ContinueEntry"] = (),
        continue_index: Optional[int] = None,
    ) -> None:
        """Highlight ring, title/subtitle labels and header/footer text drawn
        on top of the real poster-grid background image (see
        :mod:`nostalgiabox.thumbnails` and :class:`nostalgiabox.app.TVApp`,
        which swaps the player onto that image before calling this).
        ``tiles`` is real channels and game systems combined (see
        :meth:`nostalgiabox.app.TVApp._admin_tiles`), in display order.

        ``continue_entries`` (see :mod:`nostalgiabox.watch_state`) is drawn
        as a text-only row above the header, e.g. "Arthur - Sick as a Dog
        (12 min left)" - no poster art, since unlike the channel/game grid
        it can't be pre-baked into the background image (it changes with
        live watch state, not just at ``--check`` time). ``continue_index``
        highlights one of them; when it's not ``None`` the channel/game grid
        below isn't highlighted at all (the cursor is on the row, not the
        grid) - see :meth:`nostalgiabox.app.TVApp._move_browse_cursor`.
        """
        ass = _admin_browser_ass(tiles, highlight_number, continue_entries, continue_index)
        self._player.set_overlay(_ID_ADMIN, ass, CANVAS_W, CANVAS_H)
        self._expiry.pop(_ID_ADMIN, None)  # persistent until cleared

    def show_admin_episode_list(
        self, channel: "Channel", *, highlight_index: Optional[int]
    ) -> None:
        """Full-screen numbered episode list for one channel (no poster art -
        just an opaque backdrop plus text), reached by confirming a channel
        in :meth:`show_admin_browser`.
        """
        ass = _admin_episode_list_ass(channel, highlight_index)
        self._player.set_overlay(_ID_ADMIN, ass, CANVAS_W, CANVAS_H)
        self._expiry.pop(_ID_ADMIN, None)

    def clear_admin_panel(self) -> None:
        # Clears whichever admin view is currently up - the browse grid and
        # the corner panel share one overlay slot since only one is ever
        # shown at a time.
        self._player.clear_overlay(_ID_ADMIN)
        self._expiry.pop(_ID_ADMIN, None)

    def show_standby(self) -> None:
        """Persistent 'standby' notice for when the box is 'off'."""
        ass = _standby_ass(self._ui)
        self._player.set_overlay(_ID_STANDBY, ass, CANVAS_W, CANVAS_H)
        self._expiry.pop(_ID_STANDBY, None)

    def clear_standby(self) -> None:
        self._player.clear_overlay(_ID_STANDBY)
        self._expiry.pop(_ID_STANDBY, None)

    def tick(self) -> None:
        """Clear any overlays whose time is up. Call this every loop iteration."""
        now = self._clock()
        for overlay_id, when in list(self._expiry.items()):
            if now >= when:
                self._player.clear_overlay(overlay_id)
                self._expiry.pop(overlay_id, None)

    def clear_all(self) -> None:
        for overlay_id in (_ID_CHANNEL, _ID_VOLUME, _ID_STANDBY, _ID_MESSAGE, _ID_ADMIN):
            self._player.clear_overlay(overlay_id)
        self._expiry.clear()

    # -- internals ----------------------------------------------------------
    def _arm(self, overlay_id: int, duration: float) -> None:
        if duration <= 0:
            # duration 0 means "leave it until explicitly cleared"
            self._expiry.pop(overlay_id, None)
        else:
            self._expiry[overlay_id] = self._clock() + duration


# --------------------------------------------------------------------------
# Colour + style helpers
# --------------------------------------------------------------------------
def _hex_to_ass(hex_color: str, alpha: int = 0) -> str:
    """Convert ``#RRGGBB`` to an ASS ``&HAABBGGRR`` colour string."""
    h = hex_color.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _style(ui: UiConfig, *, size: int, alpha: int = 0) -> str:
    """Common ASS override tags: retro font, green fill, and a soft CRT glow."""
    color = _hex_to_ass(ui.color, alpha)
    tags = rf"\fn{ui.font}\b1\fs{size}\c{color}\1a&H{alpha:02X}&"
    if ui.glow:
        # A blurred green border reads as phosphor bloom; a faint dark edge keeps
        # it legible over bright video.
        tags += rf"\bord2\blur4\3c{color}\4c{_BLACK}\shad0"
    else:
        tags += rf"\bord2\3c{_BLACK}\shad0"
    return tags


def _dim_style(ui: UiConfig, *, size: int) -> str:
    """Like :func:`_style` but in the unlit/dim colour and no glow - used for
    the non-highlighted rows of the admin browse grid so the selected row
    reads clearly against the rest of the list.
    """
    color = _hex_to_ass(ui.dim_color)
    return rf"\fn{ui.font}\b1\fs{size}\c{color}\1a&H00&\bord2\3c{_BLACK}\shad0"


# --------------------------------------------------------------------------
# ASS builders (free functions so they are easy to unit test)
# --------------------------------------------------------------------------
def _channel_bug_ass(number: int, name: str, ui: UiConfig) -> str:
    """Green digital 'CH 03' + show name, flashed inside the top-right of the frame."""
    num = f"{number:02d}"
    number_line = (
        rf"{{\an9\pos({_IX1},{_IY0}){_style(ui, size=88)}}}CH {num}"
    )
    name_line = (
        rf"{{\an9\pos({_IX1},{_IY0 + 104}){_style(ui, size=40)}}}{_escape(name)}"
    )
    return "\n".join([number_line, name_line])


def _volume_ass(level: int, muted: bool, ui: UiConfig) -> str:
    """A 'Volume' label with solid green bars (level) then green dots (remainder)."""
    level = max(0, min(100, int(level)))
    segments = 20
    filled = 0 if muted else round(level / 100 * segments)

    bar_w = 16
    pitch = 38
    bar_h = 48
    total_w = (segments - 1) * pitch + bar_w
    x0 = _FRAME_CX - total_w // 2          # centre the bar within the 4:3 frame
    row_top = _IY1 - bar_h                  # sit just above the bottom safe edge
    dot_r = 6
    green = _hex_to_ass(ui.color)

    label = "Mute" if muted else "Volume"
    parts = [
        rf"{{\an7\pos({x0},{row_top - 62}){_style(ui, size=48)}}}{label}"
    ]

    for i in range(segments):
        cx = x0 + i * pitch + bar_w / 2
        if i < filled:
            parts.append(
                _filled_rect(x=x0 + i * pitch, y=row_top, w=bar_w, h=bar_h, fill=green)
            )
        else:
            parts.append(_dot(cx=cx, cy=row_top + bar_h / 2, r=dot_r, fill=green))
    return "\n".join(parts)


def _message_ass(text: str, ui: UiConfig) -> str:
    """A centred green digital message (channel entry, 'NO SIGNAL', etc.)."""
    return rf"{{\an8\pos({_FRAME_CX},{_IY0}){_style(ui, size=60)}}}{_escape(text)}"


def _standby_ass(ui: UiConfig) -> str:
    return rf"{{\an5\pos({_FRAME_CX},{CANVAS_H // 2}){_style(ui, size=72)}}}STANDBY"


# --------------------------------------------------------------------------
# Admin mode: modern (Netflix-ish) styling, deliberately separate from the
# retro CRT look above - kid-facing overlays never use any of this. No glow/
# blur (flat, not phosphor-bloom), a bundled sans-serif instead of the retro
# terminal font, and a dark neutral palette instead of CRT green.
# --------------------------------------------------------------------------
_ADMIN_FONT = "Roboto"  # bundled sans-serif; see assets/fonts/Roboto-LICENSE.txt (Apache-2.0)
_ADMIN_WHITE = "&H00FFFFFF"
_ADMIN_DIM = "&H00B3B3B3"
_ADMIN_MUTED = "&H00737373"
_ADMIN_BG = "&H00141414"


def _admin_style(*, size: int, color: str = _ADMIN_WHITE, bold: bool = True) -> str:
    b = 1 if bold else 0
    return rf"\fn{_ADMIN_FONT}\b{b}\fs{size}\c{color}\1a&H00&\bord0\shad0"


def _admin_panel_ass(lineup: "ChannelLineup", paused: bool, ui: UiConfig) -> str:
    """A small, dense readout in the top-left: every channel with its episode
    count (current channel marked), plus the pause state - the grown-ups-only
    overview the kid remote never shows.
    """
    current = lineup.current.number
    lines = [("PAUSED" if paused else "ADMIN", _ADMIN_WHITE, True)]
    for channel in lineup:
        marker = "> " if channel.number == current else "   "
        count = len(channel.episodes)
        ep_label = "ep" if count == 1 else "eps"
        text = f"{marker}CH {channel.number:02d}  {channel.name}  ({count} {ep_label})"
        lines.append((text, _ADMIN_WHITE if channel.number == current else _ADMIN_DIM, False))
    row_h = 32
    parts = []
    for i, (text, color, bold) in enumerate(lines):
        y = _IY0 + i * row_h
        style = _admin_style(size=24, color=color, bold=bold)
        parts.append(rf"{{\an7\pos({_IX0},{y}){style}}}{_escape(text)}")
    return "\n".join(parts)


# "Continue Watching" text row (UKE-29): drawn in canvas-space safe-area
# coordinates (like the header/footer, not the tile grid's local image
# space) since - unlike the poster grid - it's never baked into the
# background image; see thumbnails.GRID_HEADER_H for why the header
# reserves room for it regardless of whether any entries exist on a given
# run. Text-only, no poster art (the chosen "minimalist" visual treatment -
# also sidesteps needing live per-episode poster generation for state that
# changes on every watch, not just at ``--check`` time).
_CONTINUE_LIMIT = 3
_CONTINUE_LABEL_Y = _IY0 + 44
_CONTINUE_ROW_Y = _IY0 + 68
_CONTINUE_CHIP_H = 58
_CONTINUE_GAP = 24
_CONTINUE_CHIP_W = ((_IX1 - _IX0) - _CONTINUE_GAP * (_CONTINUE_LIMIT - 1)) // _CONTINUE_LIMIT


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "\u2026"


def _admin_browser_ass(
    tiles: "Sequence[Channel]",
    highlight_number: Optional[int],
    continue_entries: "Sequence[ContinueEntry]" = (),
    continue_index: Optional[int] = None,
) -> str:
    """Header, per-section "Shows"/"Games" swimlane labels, per-tile title/
    count labels (positioned to sit right under each poster - see
    :func:`nostalgiabox.thumbnails.admin_section_layout` - a highlight ring
    around the selected one, and a footer hint. The posters themselves are
    the background image this draws on top of, not drawn here. ``tiles`` is
    real channels and game systems combined.

    ``continue_entries`` draws the "Continue Watching" row above the header
    (see the constants just above); when ``continue_index`` is set, that's
    where the cursor is, and no grid tile is highlighted at all.
    """
    channels = list(tiles)
    header = "Select a channel"
    footer = "\u2190\u2191\u2193\u2192 move      mute select      hold power exit"
    parts: List[str] = []

    if continue_entries:
        parts.append(
            rf"{{\an7\pos({_IX0},{_CONTINUE_LABEL_Y}){_admin_style(size=18, color=_ADMIN_MUTED, bold=False)}}}"
            "Continue Watching"
        )
        for i, entry in enumerate(continue_entries[:_CONTINUE_LIMIT]):
            x = _IX0 + i * (_CONTINUE_CHIP_W + _CONTINUE_GAP)
            selected = i == continue_index
            if selected:
                parts.append(
                    _outline_rect(
                        x=x - 8,
                        y=_CONTINUE_ROW_Y - 8,
                        w=_CONTINUE_CHIP_W + 16,
                        h=_CONTINUE_CHIP_H + 16,
                        color=_ADMIN_WHITE,
                        thickness=3,
                    )
                )
            title_color = _ADMIN_WHITE if selected else _ADMIN_DIM
            title = _truncate(f"{entry.channel_name} - {entry.title}", 30)
            subtitle = f"{entry.minutes_left} min left"
            parts.append(rf"{{\an7\pos({x},{_CONTINUE_ROW_Y}){_admin_style(size=20, color=title_color)}}}{_escape(title)}")
            parts.append(
                rf"{{\an7\pos({x},{_CONTINUE_ROW_Y + 28}){_admin_style(size=16, color=_ADMIN_MUTED, bold=False)}}}{_escape(subtitle)}"
            )

    parts.append(rf"{{\an7\pos({_IX0},{_IY0}){_admin_style(size=34)}}}{_escape(header)}")
    for section in admin_section_layout(channels):
        if not section.tiles:
            continue
        label_x = _FRAME_X0 + section.tiles[0].x
        parts.append(
            rf"{{\an7\pos({label_x},{section.label_y}){_admin_style(size=20, color=_ADMIN_MUTED, bold=False)}}}"
            f"{_escape(section.title)}"
        )
        for tile in section.tiles:
            channel = tile.channel
            x, y = _FRAME_X0 + tile.x, tile.y
            selected = continue_index is None and channel.number == highlight_number
            if selected:
                parts.append(
                    _outline_rect(x=x - 4, y=y - 4, w=tile.w + 8, h=tile.h + 8, color=_ADMIN_WHITE, thickness=3)
                )
            count = len(channel.episodes)
            subtitle = f"{count} {_item_label(channel, count)}"
            title = f"CH {channel.number:02d}  {channel.name}"
            title_color = _ADMIN_WHITE if selected else _ADMIN_DIM
            parts.append(
                rf"{{\an7\pos({x},{y + tile.h + 8}){_admin_style(size=22, color=title_color)}}}{_escape(title)}"
            )
            parts.append(
                rf"{{\an7\pos({x},{y + tile.h + 34}){_admin_style(size=18, color=_ADMIN_MUTED, bold=False)}}}"
                f"{_escape(subtitle)}"
            )
    parts.append(rf"{{\an2\pos({_FRAME_CX},{_IY1}){_admin_style(size=20, color=_ADMIN_MUTED, bold=False)}}}{_escape(footer)}")
    return "\n".join(parts)


def _item_label(channel: "Channel", count: int) -> str:
    """'ep'/'eps' for a show, 'game'/'games' for a game system - the noun the
    count on a browse tile/list refers to."""
    if channel.config.kind == "game":
        return "game" if count == 1 else "games"
    return "ep" if count == 1 else "eps"


_EPISODE_ROW_H = 40
_EPISODE_LIST_TOP = _IY0 + 96
_EPISODE_LIST_BOTTOM = _IY1 - 44  # leave room for the footer hint below
_EPISODE_VISIBLE_ROWS = max(1, (_EPISODE_LIST_BOTTOM - _EPISODE_LIST_TOP) // _EPISODE_ROW_H)


def _episode_scroll_offset(total: int, highlight_index: Optional[int]) -> int:
    """First visible row index for the episode list: keeps the highlighted
    episode roughly centered in the visible window, clamped so the window
    never scrolls past the start/end of the list. With few enough episodes
    to fit on screen this is always 0 (no scrolling needed).
    """
    if total <= _EPISODE_VISIBLE_ROWS:
        return 0
    index = highlight_index or 0
    offset = index - _EPISODE_VISIBLE_ROWS // 2
    max_offset = total - _EPISODE_VISIBLE_ROWS
    return max(0, min(offset, max_offset))


def _admin_episode_list_ass(channel: "Channel", highlight_index: Optional[int]) -> str:
    """Full-screen numbered episode list for one channel: an opaque dark
    backdrop (there's no poster image behind this screen, unlike the show
    grid) plus a header, the numbered rows, and a footer hint.

    Only :data:`_EPISODE_VISIBLE_ROWS` rows fit on screen at once, so long
    lists scroll to keep the highlighted episode in view (see
    :func:`_episode_scroll_offset`), with small "more above/below" hints
    when the list is scrolled.
    """
    backdrop = _filled_rect(x=_FRAME_X0, y=0, w=_FRAME_W, h=CANVAS_H, fill=_ADMIN_BG)
    header = _escape(channel.name)
    total = len(channel.episodes)
    verb = "Select a game" if channel.config.kind == "game" else "Select an episode"
    subheader = verb if total <= 1 else f"{verb}  ({(highlight_index or 0) + 1} of {total})"
    footer = "\u2191\u2193 move      mute select      power back"

    parts = [backdrop]
    parts.append(rf"{{\an7\pos({_IX0},{_IY0}){_admin_style(size=34)}}}{header}")
    parts.append(rf"{{\an7\pos({_IX0},{_IY0 + 44}){_admin_style(size=20, color=_ADMIN_MUTED, bold=False)}}}{_escape(subheader)}")

    offset = _episode_scroll_offset(total, highlight_index)
    visible = channel.episodes[offset : offset + _EPISODE_VISIBLE_ROWS]
    for row, path in enumerate(visible):
        i = offset + row
        selected = i == highlight_index
        y = _EPISODE_LIST_TOP + row * _EPISODE_ROW_H
        color = _ADMIN_WHITE if selected else _ADMIN_DIM
        label = f"{i + 1}.  {episode_title(path)}"
        parts.append(rf"{{\an7\pos({_IX0},{y}){_admin_style(size=24, color=color, bold=selected)}}}{_escape(label)}")

    if offset > 0:
        hint = f"\u25b2 {offset} more"
        parts.append(rf"{{\an7\pos({_IX0},{_EPISODE_LIST_TOP - 30}){_admin_style(size=16, color=_ADMIN_MUTED, bold=False)}}}{_escape(hint)}")
    remaining = total - (offset + len(visible))
    if remaining > 0:
        hint = f"\u25bc {remaining} more"
        y = _EPISODE_LIST_TOP + len(visible) * _EPISODE_ROW_H + 4
        parts.append(rf"{{\an7\pos({_IX0},{y}){_admin_style(size=16, color=_ADMIN_MUTED, bold=False)}}}{_escape(hint)}")

    parts.append(rf"{{\an2\pos({_FRAME_CX},{_IY1}){_admin_style(size=20, color=_ADMIN_MUTED, bold=False)}}}{_escape(footer)}")
    return "\n".join(parts)



def _filled_rect(*, x: float, y: float, w: float, h: float, fill: str) -> str:
    """An ASS drawing (\\p1) filled rectangle at absolute canvas coordinates."""
    x, y = round(x), round(y)
    w, h = round(w), round(h)
    draw = f"m 0 0 l {w} 0 l {w} {h} l 0 {h}"
    return rf"{{\an7\pos({x},{y})\p1\c{fill}\1a&H00&\bord0\shad0}}{draw}{{\p0}}"


def _outline_rect(*, x: float, y: float, w: float, h: float, color: str, thickness: int) -> str:
    """An ASS drawing: an unfilled rectangle outline - the admin grid's
    selection ring around the highlighted poster tile."""
    x, y = round(x), round(y)
    w, h = round(w), round(h)
    draw = f"m 0 0 l {w} 0 l {w} {h} l 0 {h}"
    return (
        rf"{{\an7\pos({x},{y})\p1\1a&HFF&\3c{color}\bord{thickness}\shad0}}"
        rf"{draw}{{\p0}}"
    )


def _dot(*, cx: float, cy: float, r: float, fill: str) -> str:
    """A small filled circle centred at (cx, cy) using 4 bezier arcs."""
    c = 0.5523 * r  # magic constant to approximate a circle with cubic beziers
    x, y = round(cx), round(cy)
    r = round(r, 2)
    c = round(c, 2)
    path = (
        f"m 0 {-r} "
        f"b {c} {-r} {r} {-c} {r} 0 "
        f"b {r} {c} {c} {r} 0 {r} "
        f"b {-c} {r} {-r} {c} {-r} 0 "
        f"b {-r} {-c} {-c} {-r} 0 {-r}"
    )
    return rf"{{\an5\pos({x},{y})\p1\c{fill}\1a&H00&\bord0\shad0}}{path}{{\p0}}"


def _escape(text: str) -> str:
    """Escape characters that are meaningful inside an ASS override block."""
    return text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")


__all__ = ["OverlayManager", "CANVAS_W", "CANVAS_H"]
