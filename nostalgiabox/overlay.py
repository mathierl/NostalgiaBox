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

The admin/developer view (poster grid, episode list, Insights) is drawn here
too, in a separate modern (Netflix-ish) style, deliberately distinct from the
retro CRT look above - the kid-facing overlays never use any of this. UKE-29
briefly moved this to a real browser (Chromium/cage) for richer visuals, then
reverted: handing the display to a second process that has to fight mpv for
DRM ownership on every open/close reliably segfaulted the whole box on real
hardware. Back to ASS + a single pre-composed poster-grid background image
(see :mod:`nostalgiabox.thumbnails`) - one process, one owner of the display,
no race condition.

Real-hardware follow-up feedback (UKE-29) also found the admin UI was
confined to the same 4:3 "safe area" every actual show plays in, wasting
about a quarter of a widescreen TV - fixed by rendering it at the *full*
canvas width instead (see the ``_ADMIN_*`` constants below, vs. the
``_FRAME_*``/``_IX*``/``_IY*`` ones the retro overlays still use, since real
video *does* stay pillarboxed - see :mod:`nostalgiabox.thumbnails`'s module
docstring). The grid can also now be taller than one screen and scrolls
(``scroll_y`` below) rather than shrinking tiles to cram everything in.
"""

from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional, Sequence

from .channel import Channel, ChannelLineup, episode_title, item_label
from .config import Config, UiConfig
from .player import Player
from .thumbnails import (
    EVERGREEN_GAP_ABOVE,
    EVERGREEN_ROW_H,
    GRID_FOOTER_H,
    GRID_H,
    GRID_HEADER_H,
    admin_section_layout,
    scrollable_content_height,
    sections_bottom,
)
from .watch_state import ContinueEntry, InsightsSummary, WatchState

# Virtual canvas the overlays are laid out on. This maps to the WHOLE display
# (a 16:9 TV), so mpv scales it to whatever the screen is. Also the exact
# size the admin-mode background image is composed at (see
# nostalgiabox.thumbnails.GRID_W/GRID_H) - the two must be kept equal, since
# that image is shown with the usual 4:3 pillarbox filter bypassed
# (Player.play_loop(use_frame_filter=False)) specifically so it can fill
# this whole canvas edge to edge.
CANVAS_W = 1280
CANVAS_H = 720

# The video is forced into a 4:3 frame centred on the 16:9 canvas (see
# MpvPlayer.force_4_3). We lay the RETRO overlays out *inside* that 4:3 frame -
# with a small safe-area inset so nothing sits under the CRT's rounded corners -
# so the green readouts always sit over the picture, never out in the black
# pillarbox bars. Real shows/filler clips are the only things still pillarboxed
# (see the module docstring) - the admin UI below uses the full canvas instead.
_FRAME_W = int(round(CANVAS_H * 4 / 3))        # 960
_FRAME_X0 = (CANVAS_W - _FRAME_W) // 2          # 160
_FRAME_X1 = _FRAME_X0 + _FRAME_W                # 1120
_FRAME_CX = (_FRAME_X0 + _FRAME_X1) // 2        # 640
_SAFE = 0.06
_IX0 = _FRAME_X0 + int(_FRAME_W * _SAFE)        # ~217  (left safe edge)
_IX1 = _FRAME_X1 - int(_FRAME_W * _SAFE)        # ~1062 (right safe edge)
_IY0 = int(CANVAS_H * _SAFE)                     # ~43   (top safe edge)
_IY1 = CANVAS_H - int(CANVAS_H * _SAFE)          # ~677  (bottom safe edge)

# Admin UI safe area (UKE-29): the FULL canvas width, not the 4:3 frame inset
# above - matches nostalgiabox.thumbnails' own _GRID_MARGIN_X so text lines up
# with the poster tiles pasted into the background image at the same margin.
_ADMIN_X0 = 56
_ADMIN_X1 = CANVAS_W - 56       # 1224
_ADMIN_CX = CANVAS_W // 2        # 640
_ADMIN_Y0 = 32
_ADMIN_Y1 = CANVAS_H - 32        # 688

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

    def rebind_player(self, player: Player) -> None:
        """Point future overlay calls at a newly (re)created Player instance.

        TVApp._reopen_player() swaps in a fresh mpv instance after RetroArch
        hands the display back (the old one was already terminated - see
        Player.close()). Without calling this too, every overlay method
        below would keep silently talking to that dead instance for the
        rest of the process's life: MpvPlayer's overlay calls all swallow
        exceptions internally (see player.py), so nothing crashes - the
        channel bug, volume bar, and admin overlays just silently stop
        appearing after the first RetroArch handoff.
        """
        self._player = player

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

    def show_adult_mode_status(self, *, on: bool) -> None:
        """Brief transient confirmation that Adult Mode turned on/off - the
        one thing (besides the seek/volume/pause OSD messages, which already
        use show_message) worth a flash of feedback outside the grid itself.
        Deliberately NOT persistent (UKE-29): the old always-on-screen corner
        panel (every channel + PAUSED/ADMIN, glued to the picture until you
        fully exited admin mode) was exactly the clutter that got reported as
        a bug - Adult Mode is designed to leave nothing lingering on screen.
        """
        self.show_message("Adult Mode: ON" if on else "Adult Mode: OFF")

    def show_admin_browser(
        self,
        tiles: Sequence["Channel"],
        *,
        highlight_number: Optional[int],
        continue_entries: Sequence["ContinueEntry"] = (),
        continue_index: Optional[int] = None,
        insights_selected: bool = False,
        adult_mode: bool = False,
        adult_toggle_selected: bool = False,
        open_retroarch_selected: bool = False,
        scroll_y: int = 0,
    ) -> None:
        """Highlight ring, title/subtitle labels and header/footer text drawn
        on top of the real poster-grid background image (see
        :mod:`nostalgiabox.thumbnails` and :class:`nostalgiabox.app.TVApp`,
        which swaps the player onto that image before calling this).
        ``tiles`` is real channels, in display order.

        ``continue_entries`` (see :mod:`nostalgiabox.watch_state`) is drawn
        as a text-only row above the header, e.g. "Arthur - Sick as a Dog
        (12 min left)" - no poster art, since unlike the channel grid it
        can't be pre-baked into the background image (it changes with live
        watch state, not just at ``--check`` time). ``continue_index``
        highlights one of them; when it's not ``None`` the channel grid
        below isn't highlighted at all (the cursor is on the row, not the
        grid) - see :meth:`nostalgiabox.app.TVApp._move_browse_cursor`.
        ``insights_selected``/``adult_toggle_selected``/
        ``open_retroarch_selected`` highlight the evergreen rows at the
        bottom instead (UKE-29) - mutually exclusive with everything else
        and each other. ``adult_mode`` reflects the toggle's current
        ON/OFF state (not selection) - the Open RetroArch row (UKE-29 v2)
        only ever draws while it's on; see :func:`_admin_browser_ass`.

        ``scroll_y`` (UKE-29) is how far the *body* below the fixed header/
        Continue-Watching row has scrolled - see
        :meth:`nostalgiabox.app.TVApp._sync_admin_scroll`, which keeps this
        in sync with the matching crop of the background image
        (:func:`nostalgiabox.thumbnails.crop_viewport`) so the highlight
        ring and labels always land exactly on the posters under them.
        """
        ass = _admin_browser_ass(
            tiles,
            highlight_number,
            continue_entries,
            continue_index,
            insights_selected,
            adult_mode,
            adult_toggle_selected,
            open_retroarch_selected,
            scroll_y,
        )
        self._player.set_overlay(_ID_ADMIN, ass, CANVAS_W, CANVAS_H)
        self._expiry.pop(_ID_ADMIN, None)  # persistent until cleared

    def show_admin_episode_list(
        self,
        channel: "Channel",
        *,
        highlight_index: Optional[int],
        watch_state: Optional["WatchState"] = None,
    ) -> None:
        """Full-screen numbered episode list for one channel (no poster art -
        just an opaque backdrop plus text), reached by confirming a channel
        in :meth:`show_admin_browser`. ``watch_state`` (UKE-29), if given,
        adds a watched checkmark / in-progress marker next to each episode.
        """
        ass = _admin_episode_list_ass(channel, highlight_index, watch_state)
        self._player.set_overlay(_ID_ADMIN, ass, CANVAS_W, CANVAS_H)
        self._expiry.pop(_ID_ADMIN, None)

    def show_admin_insights(
        self, summary: "InsightsSummary", *, suggestions: Sequence[str] = ()
    ) -> None:
        """Full-screen, read-only watch-stats view (UKE-29): totals, a
        "favorite" channel, per-channel completion progress, a
        recent-activity feed, and text-only similar-show suggestions for the
        favorite (see :mod:`nostalgiabox.recommendations`) - reached by
        confirming the evergreen Insights tile at the bottom of the show
        grid (see :meth:`show_admin_browser`).
        """
        ass = _admin_insights_ass(summary, suggestions)
        self._player.set_overlay(_ID_ADMIN, ass, CANVAS_W, CANVAS_H)
        self._expiry.pop(_ID_ADMIN, None)

    def clear_admin_panel(self) -> None:
        # Clears whichever admin view is currently up - the browse grid,
        # episode list, and Insights share one overlay slot since only one
        # is ever shown at a time. Also what ends a browsing session: unlike
        # the old always-on-screen corner panel this replaced (UKE-29), once
        # this is cleared nothing admin-related is left on screen at all,
        # even in Adult Mode - see show_adult_mode_status for the (brief,
        # transient) feedback that replaces it.
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
_ADMIN_ACCENT = "&H0047D1FF"  # a warm amber (BGR) - favorite/highlight accents
_ADMIN_BG = "&H00141414"


def _admin_style(*, size: int, color: str = _ADMIN_WHITE, bold: bool = True) -> str:
    b = 1 if bold else 0
    return rf"\fn{_ADMIN_FONT}\b{b}\fs{size}\c{color}\1a&H00&\bord0\shad0"


# "Continue Watching" text row (UKE-29): drawn in canvas-space admin-safe-
# area coordinates (like the header/footer, not the tile grid's local image
# space) since - unlike the poster grid - it's never baked into the
# background image; see thumbnails.GRID_HEADER_H for why the header
# reserves room for it regardless of whether any entries exist on a given
# run. Text-only, no poster art (the chosen "minimalist" visual treatment -
# also sidesteps needing live per-episode poster generation for state that
# changes on every watch, not just at ``--check`` time). Pinned - along with
# the header text - above where scroll_y starts affecting anything (see
# GRID_HEADER_H / crop_viewport).
_CONTINUE_LIMIT = 3
_CONTINUE_LABEL_Y = _ADMIN_Y0 + 44
_CONTINUE_ROW_Y = _ADMIN_Y0 + 68
_CONTINUE_CHIP_H = 58
_CONTINUE_GAP = 24
_CONTINUE_CHIP_W = ((_ADMIN_X1 - _ADMIN_X0) - _CONTINUE_GAP * (_CONTINUE_LIMIT - 1)) // _CONTINUE_LIMIT

# The two evergreen rows below the last real section (UKE-29): Watch
# Insights, then the Adult Mode toggle. Drawn in canvas-space admin-safe-area
# coordinates like the Continue Watching row above (neither is part of the
# pre-baked poster-grid background image either - Insights is always
# present regardless of config/watch state, and Adult Mode's on/off state is
# live, so there's nothing to bake ahead of time for either). Unlike the
# header/Continue Watching row these DO scroll (see scroll_y) - they sit
# right after the last section, not at a fixed screen position.


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _admin_browser_ass(
    tiles: "Sequence[Channel]",
    highlight_number: Optional[int],
    continue_entries: "Sequence[ContinueEntry]" = (),
    continue_index: Optional[int] = None,
    insights_selected: bool = False,
    adult_mode: bool = False,
    adult_toggle_selected: bool = False,
    open_retroarch_selected: bool = False,
    scroll_y: int = 0,
) -> str:
    """Header, the "Shows" swimlane's label, per-tile title/count labels
    (positioned to sit right under each poster - see
    :func:`nostalgiabox.thumbnails.admin_section_layout` - a highlight ring
    around the selected one, the evergreen rows below them, and a footer
    hint. The posters themselves are the background image this draws on top
    of, not drawn here.

    ``continue_entries`` draws the "Continue Watching" row above the header
    (see the constants just above); when ``continue_index`` is set, that's
    where the cursor is. ``insights_selected``/``adult_toggle_selected``/
    ``open_retroarch_selected`` highlight the evergreen rows instead. All
    highlight states are mutually exclusive. ``scroll_y`` (UKE-29) shifts
    everything from the first section down by that many pixels - see the
    module docstring and :func:`nostalgiabox.thumbnails.crop_viewport`,
    which crops the matching window out of the background image so the two
    stay in lock-step.
    """
    channels = list(tiles)
    header = "Select a channel"
    footer = "←↑↓→ move      mute select      hold power exit"
    parts: List[str] = []

    if continue_entries:
        parts.append(
            rf"{{\an7\pos({_ADMIN_X0},{_CONTINUE_LABEL_Y}){_admin_style(size=18, color=_ADMIN_MUTED, bold=False)}}}"
            "Continue Watching"
        )
        for i, entry in enumerate(continue_entries[:_CONTINUE_LIMIT]):
            x = _ADMIN_X0 + i * (_CONTINUE_CHIP_W + _CONTINUE_GAP)
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

    parts.append(rf"{{\an7\pos({_ADMIN_X0},{_ADMIN_Y0}){_admin_style(size=34)}}}{_escape(header)}")

    body_top = GRID_HEADER_H
    body_bottom = GRID_H - GRID_FOOTER_H  # leave room for the pinned footer hint
    if scroll_y > 0:
        parts.append(
            rf"{{\an7\pos({_ADMIN_CX},{body_top - 14}){_admin_style(size=16, color=_ADMIN_MUTED, bold=False)}}}"
            "▲ more above"
        )

    for section in admin_section_layout(channels):
        if not section.tiles:
            continue
        label_y = section.label_y - scroll_y
        label_x = section.tiles[0].x
        if body_top - 30 <= label_y <= body_bottom:
            parts.append(
                rf"{{\an7\pos({label_x},{label_y}){_admin_style(size=20, color=_ADMIN_MUTED, bold=False)}}}"
                f"{_escape(section.title)}"
            )
        for tile in section.tiles:
            channel = tile.channel
            x, y = tile.x, tile.y - scroll_y
            if y + tile.h < body_top or y > body_bottom:
                continue  # fully scrolled out of view - nothing to draw
            selected = (
                continue_index is None
                and not insights_selected
                and not adult_toggle_selected
                and not open_retroarch_selected
                and channel.number == highlight_number
            )
            if selected:
                parts.append(
                    _outline_rect(x=x - 4, y=y - 4, w=tile.w + 8, h=tile.h + 8, color=_ADMIN_WHITE, thickness=3)
                )
            count = len(channel.episodes)
            subtitle = f"{count} {item_label(channel, count)}"
            title = f"CH {channel.number:02d}  {channel.name}"
            title_color = _ADMIN_WHITE if selected else _ADMIN_DIM
            parts.append(
                rf"{{\an7\pos({x},{y + tile.h + 8}){_admin_style(size=22, color=title_color)}}}{_escape(title)}"
            )
            parts.append(
                rf"{{\an7\pos({x},{y + tile.h + 34}){_admin_style(size=18, color=_ADMIN_MUTED, bold=False)}}}"
                f"{_escape(subtitle)}"
            )

    # The evergreen rows - Watch Insights, then Adult Mode, then (once Adult
    # Mode is on) Open RetroArch - always right after the real sections, in
    # that order, and (like them) subject to scroll_y. Open RetroArch's
    # vertical space is reserved even while hidden (see
    # thumbnails.scrollable_content_height) so nothing else ever shifts
    # position as Adult Mode toggles on/off.
    bottom = sections_bottom(channels)
    insights_y = bottom + EVERGREEN_GAP_ABOVE - scroll_y
    adult_y = insights_y + EVERGREEN_ROW_H + EVERGREEN_GAP_ABOVE
    retro_y = adult_y + EVERGREEN_ROW_H + EVERGREEN_GAP_ABOVE
    row_w = _ADMIN_X1 - _ADMIN_X0

    _evergreen_row(
        parts,
        x=_ADMIN_X0, y=insights_y, w=row_w,
        selected=insights_selected,
        icon="\U0001F4CA", label="Watch Insights", hint="stats & suggestions",
        visible=(body_top <= insights_y + EVERGREEN_ROW_H and insights_y <= body_bottom),
    )
    _evergreen_row(
        parts,
        x=_ADMIN_X0, y=adult_y, w=row_w,
        selected=adult_toggle_selected,
        icon="\U0001F512" if not adult_mode else "\U0001F513",
        label=f"Adult Mode: {'ON' if adult_mode else 'OFF'}",
        hint="pause, seek, subtitles - no more grown-up overlay" if adult_mode else "unlocks pause, seek & subtitles",
        visible=(body_top <= adult_y + EVERGREEN_ROW_H and adult_y <= body_bottom),
    )
    if adult_mode:
        _evergreen_row(
            parts,
            x=_ADMIN_X0, y=retro_y, w=row_w,
            selected=open_retroarch_selected,
            icon="\U0001F3AE", label="Open RetroArch", hint="games, cores & save states live in its own menu now",
            visible=(body_top <= retro_y + EVERGREEN_ROW_H and retro_y <= body_bottom),
        )

    max_scroll = max(0, scrollable_content_height(channels) - GRID_H)
    if scroll_y < max_scroll:
        parts.append(
            rf"{{\an7\pos({_ADMIN_CX},{body_bottom + 14}){_admin_style(size=16, color=_ADMIN_MUTED, bold=False)}}}"
            "▼ more below"
        )

    parts.append(rf"{{\an2\pos({_ADMIN_CX},{_ADMIN_Y1}){_admin_style(size=20, color=_ADMIN_MUTED, bold=False)}}}{_escape(footer)}")
    return "\n".join(parts)


def _evergreen_row(
    parts: List[str],
    *,
    x: int,
    y: int,
    w: int,
    selected: bool,
    icon: str,
    label: str,
    hint: str,
    visible: bool,
) -> None:
    """One of the two full-width evergreen rows below the grid (Watch
    Insights, Adult Mode) - factored out since they're identical in every
    way except their text/selection state. ``visible`` skips drawing
    entirely once scrolled fully out of view (see _admin_browser_ass).
    """
    if not visible:
        return
    if selected:
        parts.append(
            _outline_rect(x=x - 4, y=y - 4, w=w + 8, h=EVERGREEN_ROW_H + 8, color=_ADMIN_WHITE, thickness=3)
        )
    bg = _ADMIN_ACCENT if selected else "&H00303030"
    parts.append(_filled_rect(x=x, y=y, w=w, h=EVERGREEN_ROW_H, fill=bg))
    parts.append(
        rf"{{\an4\pos({x + 24},{y + EVERGREEN_ROW_H // 2}){_admin_style(size=26, color=_ADMIN_WHITE)}}}"
        f"{icon}  {_escape(label)}"
    )
    parts.append(
        rf"{{\an6\pos({x + w - 24},{y + EVERGREEN_ROW_H // 2}){_admin_style(size=18, color=_ADMIN_MUTED, bold=False)}}}"
        f"{_escape(hint)}"
    )


_EPISODE_ROW_H = 40
_EPISODE_LIST_TOP = _ADMIN_Y0 + 96
_EPISODE_LIST_BOTTOM = _ADMIN_Y1 - 44  # leave room for the footer hint below
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


_PROGRESS_GREEN = "&H004DFF5A"  # BGR - matches the retro OSD's phosphor green


def _episode_progress_marker(
    channel: "Channel", path: "Path", watch_state: Optional["WatchState"]
) -> tuple[str, str]:
    """(text, colour) marker for one episode row - a checkmark once watched,
    a percentage while in progress, or nothing at all (UKE-29).
    """
    if watch_state is None:
        return "", ""
    state = watch_state.episode_state(channel.number, channel.config.path, path)
    if state.watched:
        return "✓ Watched", _PROGRESS_GREEN
    if state.in_progress:
        return f"{int(round(state.fraction * 100))}% watched", _ADMIN_MUTED
    return "", ""


def _admin_episode_list_ass(
    channel: "Channel",
    highlight_index: Optional[int],
    watch_state: Optional["WatchState"] = None,
) -> str:
    """Full-screen numbered episode list for one channel: an opaque dark
    backdrop (there's no poster image behind this screen, unlike the show
    grid) plus a header, the numbered rows, and a footer hint.

    Only :data:`_EPISODE_VISIBLE_ROWS` rows fit on screen at once, so long
    lists scroll to keep the highlighted episode in view (see
    :func:`_episode_scroll_offset`), with small "more above/below" hints
    when the list is scrolled. ``watch_state``, if given, adds a watched
    checkmark / in-progress percentage next to each row (see
    :func:`_episode_progress_marker`).
    """
    backdrop = _filled_rect(x=0, y=0, w=CANVAS_W, h=CANVAS_H, fill=_ADMIN_BG)
    header = _escape(channel.name)
    total = len(channel.episodes)
    verb = "Select an episode"
    subheader = verb if total <= 1 else f"{verb}  ({(highlight_index or 0) + 1} of {total})"
    footer = "↑↓ move      mute select      power back"

    parts = [backdrop]
    parts.append(rf"{{\an7\pos({_ADMIN_X0},{_ADMIN_Y0}){_admin_style(size=34)}}}{header}")
    parts.append(rf"{{\an7\pos({_ADMIN_X0},{_ADMIN_Y0 + 44}){_admin_style(size=20, color=_ADMIN_MUTED, bold=False)}}}{_escape(subheader)}")

    offset = _episode_scroll_offset(total, highlight_index)
    visible = channel.episodes[offset : offset + _EPISODE_VISIBLE_ROWS]
    for row, path in enumerate(visible):
        i = offset + row
        selected = i == highlight_index
        y = _EPISODE_LIST_TOP + row * _EPISODE_ROW_H
        color = _ADMIN_WHITE if selected else _ADMIN_DIM
        label = f"{i + 1}.  {episode_title(path)}"
        parts.append(rf"{{\an7\pos({_ADMIN_X0},{y}){_admin_style(size=24, color=color, bold=selected)}}}{_escape(label)}")
        marker, marker_color = _episode_progress_marker(channel, path, watch_state)
        if marker:
            parts.append(
                rf"{{\an9\pos({_ADMIN_X1},{y}){_admin_style(size=18, color=marker_color, bold=False)}}}{_escape(marker)}"
            )

    if offset > 0:
        hint = f"▲ {offset} more"
        parts.append(rf"{{\an7\pos({_ADMIN_X0},{_EPISODE_LIST_TOP - 30}){_admin_style(size=16, color=_ADMIN_MUTED, bold=False)}}}{_escape(hint)}")
    remaining = total - (offset + len(visible))
    if remaining > 0:
        hint = f"▼ {remaining} more"
        y = _EPISODE_LIST_TOP + len(visible) * _EPISODE_ROW_H + 4
        parts.append(rf"{{\an7\pos({_ADMIN_X0},{y}){_admin_style(size=16, color=_ADMIN_MUTED, bold=False)}}}{_escape(hint)}")

    parts.append(rf"{{\an2\pos({_ADMIN_CX},{_ADMIN_Y1}){_admin_style(size=20, color=_ADMIN_MUTED, bold=False)}}}{_escape(footer)}")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Insights screen (UKE-29): totals, a "favorite", per-channel progress,
# recent activity, and text-only similar-show suggestions. No prior ASS
# version of this existed - it was designed and built directly for the
# (now-reverted) browser UI, so this is a fresh layout, not a restoration.
# --------------------------------------------------------------------------
_STAT_Y = _ADMIN_Y0 + 40
_STAT_NUMBER_SIZE = 52
_STAT_LABEL_SIZE = 16
_FAVORITE_Y = _ADMIN_Y0 + 140
_PROGRESS_LABEL_Y = _ADMIN_Y0 + 210
_PROGRESS_TOP = _ADMIN_Y0 + 240
_PROGRESS_ROW_H = 30
_PROGRESS_MAX_ROWS = 6
_PROGRESS_BAR_W = 180
_PROGRESS_BAR_H = 10
_ACTIVITY_GAP = 28
_ACTIVITY_ROW_H = 26
_ACTIVITY_MAX_ROWS = 5


def _stat_block(x: int, number: str, label: str) -> List[str]:
    return [
        rf"{{\an7\pos({x},{_STAT_Y}){_admin_style(size=_STAT_NUMBER_SIZE, color=_ADMIN_WHITE)}}}{_escape(number)}",
        rf"{{\an7\pos({x},{_STAT_Y + _STAT_NUMBER_SIZE + 6}){_admin_style(size=_STAT_LABEL_SIZE, color=_ADMIN_MUTED, bold=False)}}}{_escape(label)}",
    ]


def _admin_insights_ass(summary: "InsightsSummary", suggestions: Sequence[str] = ()) -> str:
    """Full-screen read-only stats view: an opaque backdrop (same treatment
    as the episode list - there's no poster art behind this screen) plus a
    header, three headline stats, a favorite banner with similar-show
    suggestions, per-channel completion bars, and a recent-activity feed.
    """
    parts = [_filled_rect(x=0, y=0, w=CANVAS_W, h=CANVAS_H, fill=_ADMIN_BG)]
    parts.append(rf"{{\an7\pos({_ADMIN_X0},{_ADMIN_Y0}){_admin_style(size=34)}}}Insights")

    touched = [c for c in summary.channels if c.last_played > 0]
    if not touched:
        parts.append(
            rf"{{\an5\pos({_ADMIN_CX},{CANVAS_H // 2}){_admin_style(size=24, color=_ADMIN_MUTED, bold=False)}}}"
            "Nothing watched yet - pick a show to get started!"
        )
        parts.append(
            rf"{{\an2\pos({_ADMIN_CX},{_ADMIN_Y1}){_admin_style(size=20, color=_ADMIN_MUTED, bold=False)}}}"
            "power back"
        )
        return "\n".join(parts)

    # Two headline stats, evenly spaced across the safe width.
    stat_w = (_ADMIN_X1 - _ADMIN_X0) // 2
    parts += _stat_block(_ADMIN_X0, str(summary.total_watched_minutes), "minutes watched")
    parts += _stat_block(_ADMIN_X0 + stat_w, str(summary.total_episodes_watched), "episodes watched")

    # Favorite banner + similar-show suggestions.
    if summary.favorite is not None:
        fav = summary.favorite
        parts.append(
            rf"{{\an7\pos({_ADMIN_X0},{_FAVORITE_Y}){_admin_style(size=24, color=_ADMIN_ACCENT)}}}"
            f"★ Favorite show: {_escape(fav.name)}"
        )
        if suggestions:
            text = "Similar: " + ", ".join(suggestions)
            parts.append(
                rf"{{\an7\pos({_ADMIN_X0},{_FAVORITE_Y + 30}){_admin_style(size=16, color=_ADMIN_MUTED, bold=False)}}}"
                f"{_escape(_truncate(text, 90))}"
            )

    # Per-channel completion progress - touched channels only, favorites-
    # first (same ordering rule as the "favorite" pick itself), capped so
    # this never needs to scroll.
    parts.append(
        rf"{{\an7\pos({_ADMIN_X0},{_PROGRESS_LABEL_Y}){_admin_style(size=18, color=_ADMIN_MUTED, bold=False)}}}"
        "Progress"
    )
    ranked = sorted(touched, key=lambda c: c.watched_minutes, reverse=True)
    green = _hex_to_ass("#4DFF5A")
    for row, ch in enumerate(ranked[:_PROGRESS_MAX_ROWS]):
        y = _PROGRESS_TOP + row * _PROGRESS_ROW_H
        label = _truncate(ch.name, 22)
        parts.append(rf"{{\an7\pos({_ADMIN_X0},{y}){_admin_style(size=18, color=_ADMIN_WHITE)}}}{_escape(label)}")
        bar_x = _ADMIN_X0 + 260
        fraction = 0.0 if ch.total_count <= 0 else min(1.0, ch.watched_count / ch.total_count)
        parts.append(_filled_rect(x=bar_x, y=y + 4, w=_PROGRESS_BAR_W, h=_PROGRESS_BAR_H, fill="&H00303030"))
        if fraction > 0:
            parts.append(
                _filled_rect(x=bar_x, y=y + 4, w=_PROGRESS_BAR_W * fraction, h=_PROGRESS_BAR_H, fill=green)
            )
        count_label = f"{ch.watched_count} of {ch.total_count}"
        parts.append(
            rf"{{\an7\pos({bar_x + _PROGRESS_BAR_W + 16},{y}){_admin_style(size=16, color=_ADMIN_MUTED, bold=False)}}}"
            f"{_escape(count_label)}"
        )

    # Recent activity feed, most recent first.
    activity_label_y = _PROGRESS_TOP + min(len(ranked), _PROGRESS_MAX_ROWS) * _PROGRESS_ROW_H + _ACTIVITY_GAP
    parts.append(
        rf"{{\an7\pos({_ADMIN_X0},{activity_label_y}){_admin_style(size=18, color=_ADMIN_MUTED, bold=False)}}}"
        "Recent Activity"
    )
    now = time.time()
    for row, entry in enumerate(summary.activity[:_ACTIVITY_MAX_ROWS]):
        y = activity_label_y + 28 + row * _ACTIVITY_ROW_H
        if y > _ADMIN_Y1 - _ACTIVITY_ROW_H:
            break  # ran out of room - a longer feed just gets cut off, not overflowed
        status = "" if entry.watched else " (in progress)"
        text = f"{entry.channel_name} - {_truncate(entry.title, 34)}{status}"
        parts.append(rf"{{\an7\pos({_ADMIN_X0},{y}){_admin_style(size=18, color=_ADMIN_DIM, bold=False)}}}{_escape(text)}")
        when = _relative_time(entry.when, now)
        if when:
            parts.append(
                rf"{{\an9\pos({_ADMIN_X1},{y}){_admin_style(size=16, color=_ADMIN_MUTED, bold=False)}}}{_escape(when)}"
            )

    parts.append(
        rf"{{\an2\pos({_ADMIN_CX},{_ADMIN_Y1}){_admin_style(size=20, color=_ADMIN_MUTED, bold=False)}}}power back"
    )
    return "\n".join(parts)


def _relative_time(when: float, now: float) -> str:
    """A short, human "how long ago" label (e.g. "5m ago", "3h ago", "2d
    ago") for the Insights activity feed.
    """
    if when <= 0:
        return ""
    delta = max(0.0, now - when)
    if delta < 60:
        return "just now"
    minutes = int(delta // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = int(delta // 3600)
    if hours < 24:
        return f"{hours}h ago"
    days = int(delta // 86400)
    return f"{days}d ago"


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
