"""The television itself: the state machine that ties everything together.

:class:`TVApp` owns the channel lineup, the player, the overlays and the input
queue, and turns remote-control actions into TV behaviour: changing channels
(with a burst of static and a channel banner), adjusting and muting the volume,
direct channel entry by number, an info banner, a "last channel" jump, and a
standby/off mode. When an episode ends it automatically rolls into the next one
on that channel's shuffle, so the box never stops "broadcasting".

The class is written to be testable without a display: pass it a
:class:`~nostalgiabox.player.MockPlayer` and a fake clock and you can single-step
the whole thing (see ``step`` / ``handle_event`` / ``process_pending``).

Note on the admin/developer view (UKE-29): this briefly ran as a real browser
(Chromium via `cage`) for a richer Netflix-style look, then was reverted -
handing the display to a second process that has to fight mpv for DRM
ownership on every open/close reliably segfaulted the whole box on real
hardware (repeated "Swapchain... failed test" errors from the compositor
right before each crash - a DRM master race, not a Python bug that could be
caught and handled). Back to mpv rendering everything itself: the browse
grid/episode list/Insights screen are drawn as ASS overlays (see overlay.py)
on top of a pre-composed poster-grid background image (see thumbnails.py)
that mpv just plays like any other clip - one process, one owner of the
display, no race condition. The only remaining display handoff is launching
a game via RetroArch (see _launch_game), which does have to close mpv - that
one was validated safe on real hardware independently (see
scripts/spike_mpv_retroarch_handoff.py, UKE-28).

Further real-hardware feedback (still UKE-29) led to a second pass on admin
mode itself - three distinct states rather than one overloaded flag:

* **Kid Mode** - the default. The remote behaves exactly as it always has;
  nothing admin-related is ever drawn.
* **Admin Mode** (``admin_browsing``/``admin_episode_browsing``/
  ``admin_insights_viewing`` - ``admin_mode`` is just "is any of these
  true?", see the property below) - the full-screen grid/episode-list/
  Insights browse screens, reached by holding Power as before. Purely
  modal: closing it (by picking something, or backing all the way out)
  leaves nothing on screen.
* **Adult Mode** (``adult_mode``) - a new *sticky* flag, flipped from a
  toggle row inside the grid (alongside Insights - see ``_confirm_adult_
  toggle``), that survives closing the grid and channel changes until
  turned off again (or standby). While it's on and nothing's being
  browsed, Mute/Channel-Up-Down/Info are repurposed into pause/seek/
  subtitle-toggle - a grown-up-only control surface the kid remote never
  exposes - but *unlike* the flag this replaced, nothing is left glued to
  the screen: no persistent corner panel, just the same brief OSD messages
  every other action already uses. Back/Last-Channel, repurposed the same
  way, reopens the grid straight into the current show's episode list for
  a quick episode switch (see ``_jump_last_channel``). Adult Mode is only
  ever turned off from its own toggle row inside the grid (or reset
  outright by standby, the kid-proof reset point - see _toggle_standby) -
  long-press Power (``ADMIN_TOGGLE``) still just opens/closes the grid
  itself, exactly as before.

The browse grid can also now be taller than one screen (posters keep a
fixed comfortable size and wrap onto more rows instead of shrinking - see
``nostalgiabox.thumbnails.admin_section_layout``) and scrolls vertically as
the cursor moves between rows (``_scroll_y`` / ``_sync_admin_scroll``),
cropping a screen's worth out of the pre-composed image live (see
``nostalgiabox.thumbnails.crop_viewport`` - cheap, no ffmpeg/poster work).
"""

from __future__ import annotations

import logging
import queue
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, List, Optional

from .actions import Action, InputEvent
from .channel import Channel, ChannelLineup, PlayRequest, build_game_channels, build_lineup
from .config import Config
from .input.manager import InputManager, create_backends
from .overlay import OverlayManager
from .player import END_EOF, END_ERROR, MockPlayer, Player
from .static_gen import (
    COLORBARS_FILENAME,
    DEFAULT_ASSETS_DIR,
    GLITCH_FILENAME,
    STATIC_FILENAME,
)
from .watch_state import (
    STATE_FILENAME,
    STATE_SUBDIR,
    ContinueEntry,
    WatchState,
    continue_watching,
    insights_summary,
)

log = logging.getLogger(__name__)


def _build_mpv_player(config: Config, assets_dir: Path) -> Player:
    """Construct a real MpvPlayer from config - factored out of from_config so
    it can also be used as a *factory*, called again later to reopen mpv
    after a game launch hands the display back (see TVApp._launch_game and
    scripts/spike_mpv_retroarch_handoff.py, which confirmed this handoff is
    safe on real hardware - UKE-28).
    """
    from .crt import write_shader
    from .player import MpvPlayer

    shader_path = write_shader(config.crt)
    return MpvPlayer(
        glsl_shaders=str(shader_path) if shader_path else None,
        fonts_dir=assets_dir / "fonts",
        force_4_3=config.force_4_3,
        audio_device=config.audio_device,
        gpu_context=config.gpu_context,
    )


def _default_game_launcher(core: str, rom: Path) -> int:
    """Run a game to completion via bare RetroArch, blocking until it exits
    (normally via its own F1 -> Quit RetroArch). A failed launch should never
    crash the box, so any exception is caught and treated as a non-zero exit.
    """
    try:
        result = subprocess.run(["retroarch", "-L", core, str(rom)])
        return result.returncode
    except Exception:  # noqa: BLE001
        log.exception("failed to launch game: core=%s rom=%s", core, rom)
        return 1


class TVApp:
    """The retro-TV application state machine."""

    def __init__(
        self,
        config: Config,
        player: Player,
        input_manager: InputManager,
        *,
        overlay: Optional[OverlayManager] = None,
        clock: Callable[[], float] = time.monotonic,
        assets_dir: Optional[Path] = None,
        player_factory: Optional[Callable[[], Player]] = None,
        game_launcher: Optional[Callable[[str, Path], int]] = None,
        watch_state: Optional[WatchState] = None,
        drm_handoff_delay: float = 0.0,
    ) -> None:
        self.config = config
        self.player = player
        self.input = input_manager
        self.overlay = overlay or OverlayManager(player, config, clock=clock)
        self._clock = clock
        # Per-episode/per-game watch history (UKE-29) - None by default (as
        # in most tests) means "don't track", so nothing touches disk unless
        # a caller explicitly opts in; from_config wires up a real one.
        self.watch_state = watch_state
        # Rebuilds a fresh Player after a game hands the display back (see
        # _launch_game) - only set for real hardware (from_config); with no
        # factory (dry-run / most tests) the same player instance is reused,
        # which is fine for MockPlayer and for anything that never launches
        # a game.
        self._player_factory = player_factory
        self._game_launcher = game_launcher or _default_game_launcher
        # A brief pause around the one remaining display handoff - launching
        # a game via RetroArch (see _launch_game) - between mpv releasing
        # the display and the next owner claiming it. mpv.terminate()
        # returns once its core thread has shut down, but the DRM/GBM
        # teardown underneath it isn't strictly guaranteed to be finished by
        # then; real-hardware testing this session (UKE-29) found this
        # matters a great deal for a second *long-running* DRM client
        # (Chromium/cage, since reverted), and costs nothing to also apply
        # here as cheap insurance, even though the RetroArch handoff was
        # separately validated safe without it. 0 for dry-run/tests (no
        # real DRM device to race over there) - from_config sets a real
        # value for actual hardware.
        self._drm_handoff_delay = drm_handoff_delay

        self.lineup: ChannelLineup = build_lineup(config)
        # Game systems (see UKE-28): shown in the admin browse grid alongside
        # real channels (_admin_tiles), but never part of self.lineup - the
        # kid-facing tuner must never be able to land on one.
        self.games: List[Channel] = build_game_channels(config)

        # Runtime state.
        self.volume = config.initial_volume
        self.muted = False
        # Subtitle visibility (UKE-29): toggled via Info while watching under
        # Adult Mode (see _toggle_subtitles) - starts at whatever config says.
        self.subtitles_visible = config.subtitles_default
        self.standby = False
        self.powered_off = False
        # Admin/developer view: hidden behind a long power-button hold (see
        # input/keyboard.py). See the module docstring for the full Kid /
        # Admin / Adult three-mode picture (UKE-29) - admin_mode itself is
        # now a derived property (below), not real state, so it can never
        # drift out of sync with the three browse-screen flags it reflects.
        self.paused = False
        # Admin mode has three nested browse screens. admin_browsing = True is
        # the top-level "select a channel" poster grid: Channel Up/Down move a
        # row cursor, Volume Up/Down (repurposed here only) move a column
        # cursor, Mute confirms and drills into that channel's episode list
        # (admin_episode_browsing = True): Channel Up/Down move an episode
        # cursor, Mute plays that exact episode, Power backs out to the show
        # grid instead of standby. Long-press Power always exits admin mode
        # entirely, from any of the three screens.
        self.admin_browsing = False
        self.admin_episode_browsing = False
        # The third admin-mode screen (UKE-29): the Insights view (watch-time
        # stats, a "favorite" channel, a recent-activity feed, and
        # text-only similar-show suggestions - see watch_state.insights_summary
        # and recommendations.py). Modeled the same way as the Continue
        # Watching row rather than as a real Channel - it's a single evergreen
        # entry in _section_keys, not something scanned from disk - so
        # _browse_on_insights is the marker for "cursor is on it", the same
        # role _browse_continue_index plays for that row.
        self.admin_insights_viewing = False
        self._browse_on_insights = False
        # Adult Mode (UKE-29, see module docstring): sticky, independent of
        # whether the grid is currently open. Flipped via the evergreen
        # toggle row right below Insights - _browse_on_adult_toggle is that
        # row's "cursor is on it" marker, same idea as _browse_on_insights.
        self.adult_mode = False
        self._browse_on_adult_toggle = False
        # How far the grid has scrolled (UKE-29) - see _sync_admin_scroll,
        # called any time the cursor moves to a row that might not already
        # be on screen. Reset to 0 whenever the grid is (re)opened.
        self._scroll_y: int = 0
        self._browse_number: Optional[int] = None
        self._browse_episode_number: Optional[int] = None
        self._browse_episode_index: int = 0
        # "Continue Watching" row (UKE-29): a text-only row of in-progress
        # episodes shown above the channel/game grid. _continue_entries is
        # (re)computed each time the grid is (re)entered, not on every
        # keypress - see _refresh_continue_entries. _browse_continue_index
        # is None whenever the cursor is on the grid itself (the common
        # case, and the only state possible before this feature existed);
        # it's only set while the cursor is actually on this row.
        self._continue_entries: List[ContinueEntry] = []
        self._browse_continue_index: Optional[int] = None
        # What was playing right before admin mode opened, so browsing
        # without picking anything new resumes exactly where it left off
        # (the show grid replaces the live picture with poster art - see
        # _show_admin_grid_background). Cleared once a new episode is
        # actually chosen, since there's then nothing to resume.
        self._pre_admin_path: Optional[Path] = None
        self._pre_admin_pos: float = 0.0
        self._playing_path: Optional[Path] = None
        self._last_channel_number: Optional[int] = None
        self._running = False

        # Direct channel entry ("type 1 then 2 -> channel 12").
        self._digit_buffer = ""
        self._digit_deadline = 0.0
        self._digit_entry_timeout = 2.0

        # Pending "bridge" switch: keep the old show playing until this deadline,
        # then cut to the channel that was preloaded. The channel banner is shown
        # at the moment of the cut-over, not when the button is pressed.
        self._switch_deadline: Optional[float] = None
        self._pending_banner: Optional[tuple[int, str]] = None

        # Playback-finished events from the player (may arrive on any thread).
        self._ended: "queue.Queue[str]" = queue.Queue()
        self.player.on_end = self._ended.put

        # Filler assets.
        self._assets_dir = assets_dir or config.assets_dir or DEFAULT_ASSETS_DIR
        self._colorbars_path = self._resolve_asset(COLORBARS_FILENAME)
        # The channel-change transition clip depends on the configured effect.
        self._transition_path = self._resolve_transition_asset()

    @property
    def admin_mode(self) -> bool:
        """True whenever one of the three browse screens (grid/episode-list/
        Insights) is open - derived, not stored, so it can never disagree
        with them (UKE-29; see the module docstring for the full Kid/Admin/
        Adult picture). Read-only from outside; flip the individual
        ``admin_browsing``/``admin_episode_browsing``/``admin_insights_
        viewing`` flags (via ``_toggle_admin_mode`` and friends) instead.
        """
        return self.admin_browsing or self.admin_episode_browsing or self.admin_insights_viewing

    # -- construction -------------------------------------------------------
    @classmethod
    def from_config(
        cls,
        config: Config,
        *,
        player: Optional[Player] = None,
        input_manager: Optional[InputManager] = None,
        dry_run: bool = False,
        assets_dir: Optional[Path] = None,
    ) -> "TVApp":
        """Build a fully wired app, creating real hardware backends by default.

        ``dry_run`` swaps in a :class:`MockPlayer` and disables all real input
        backends (a stdin backend is added if a TTY is available), which is how
        the box can be exercised on a development machine.
        """
        player_factory = None
        if player is None:
            if dry_run:
                player = MockPlayer(verbose=True)
            else:
                assets = assets_dir or config.assets_dir or DEFAULT_ASSETS_DIR
                player = _build_mpv_player(config, assets)
                # Lets _launch_game reopen mpv with the exact same options
                # after a game hands the display back.
                player_factory = lambda: _build_mpv_player(config, assets)

        if input_manager is None:
            if dry_run:
                backends = create_backends({"keyboard": False, "cec": False, "stdin": True})
            else:
                backends = create_backends(
                    config.input_options,
                    admin_hold_seconds=(
                        config.admin_hold_seconds if config.admin_mode_enabled else None
                    ),
                )
            input_manager = InputManager(backends)

        assets = assets_dir or config.assets_dir or DEFAULT_ASSETS_DIR
        watch_state = WatchState(assets / STATE_SUBDIR / STATE_FILENAME)

        return cls(
            config,
            player,
            input_manager,
            assets_dir=assets_dir,
            player_factory=player_factory,
            watch_state=watch_state,
            drm_handoff_delay=0.0 if dry_run else 0.5,
        )

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        """Power on: set volume, start input, and tune to the first channel."""
        self.player.set_volume(self.volume)
        self.player.set_mute(self.muted)
        self.player.set_subtitle_visible(self.subtitles_visible)
        self.input.start()
        self._select_start_channel()
        self.tune_current(show_static=False)

    def run(self) -> None:
        """Run the blocking main loop until a QUIT action is received."""
        self.start()
        self._running = True
        log.info("NostalgiaBox is on the air. %d channels.", len(self.lineup))
        try:
            while self._running:
                self.step(block=True)
        except KeyboardInterrupt:  # pragma: no cover - interactive convenience
            log.info("interrupted; shutting down")
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self._running = False
        try:
            self.overlay.clear_all()
        except Exception:  # noqa: BLE001
            pass
        self.input.stop()
        self.player.close()

    # -- main-loop step (small and testable) --------------------------------
    def step(self, *, block: bool = False, timeout: float = 0.1) -> None:
        """Advance the state machine by one iteration.

        Handles overlay expiry, channel-entry timeouts, finished episodes, and
        at most one queued input event.
        """
        now = self._clock()
        self.overlay.tick()
        self._maybe_commit_switch(now)
        self._maybe_commit_digits(now)
        self._drain_playback_events()

        event = self.input.get(timeout=timeout if block else 0.0)
        if event is not None:
            self.handle_event(event)

    def _maybe_commit_switch(self, now: float) -> None:
        """Cut over to the preloaded channel once the bridge window has elapsed."""
        if self._switch_deadline is not None and now >= self._switch_deadline:
            self._switch_deadline = None
            self.player.commit_switch()
            # Flash the channel banner right as the picture actually changes.
            if self._pending_banner is not None:
                self.overlay.show_channel_bug(*self._pending_banner)
                self._pending_banner = None

    # -- input handling -----------------------------------------------------
    def handle_event(self, event: InputEvent) -> None:
        action = event.action

        if action == Action.QUIT:
            self._running = False
            return
        if action == Action.POWER:
            if self.admin_episode_browsing:
                self._admin_back_to_shows()
                return
            if self.admin_insights_viewing:
                self._admin_back_from_insights()
                return
            self._toggle_standby()
            return

        # While in standby, ignore everything except POWER/QUIT (handled above).
        if self.standby:
            return

        handlers = {
            Action.CHANNEL_UP: self._channel_up,
            Action.CHANNEL_DOWN: self._channel_down,
            Action.VOLUME_UP: self._volume_up,
            Action.VOLUME_DOWN: self._volume_down,
            Action.MUTE: self._toggle_mute,
            Action.INFO: self._show_info,
            Action.LAST_CHANNEL: self._jump_last_channel,
            Action.ENTER: self._confirm_digits,
            Action.ADMIN_TOGGLE: self._toggle_admin_mode,
        }
        if action == Action.DIGIT:
            self._push_digit(event.value or 0)
        else:
            handler = handlers.get(action)
            if handler is not None:
                handler()

    # -- channel changing ---------------------------------------------------
    def _channel_up(self) -> None:
        if self.admin_episode_browsing:
            self._move_episode_cursor(-1)
            return
        if self.admin_browsing:
            self._move_browse_cursor(drow=-1)
            return
        if self.admin_insights_viewing:
            # Read-only screen - nothing to move/change.
            return
        if self.adult_mode:
            # Once something's actually playing under Adult Mode (not a
            # browse screen), Channel Up/Down are repurposed into seek - a
            # grown-up-only control, same spirit as Mute becoming pause/play
            # here (UKE-29; see the module docstring for the Kid/Admin/Adult
            # picture - gated on adult_mode now, not the old admin_mode flag,
            # which only ever means "a browse screen is open").
            self._seek(self.config.admin_seek_seconds)
            return
        self._remember_position()
        self._last_channel_number = self.lineup.current.number
        self.lineup.up()
        self.tune_current()

    def _channel_down(self) -> None:
        if self.admin_episode_browsing:
            self._move_episode_cursor(1)
            return
        if self.admin_browsing:
            self._move_browse_cursor(drow=1)
            return
        if self.admin_insights_viewing:
            return
        if self.adult_mode:
            self._seek(-self.config.admin_seek_seconds)
            return
        self._remember_position()
        self._last_channel_number = self.lineup.current.number
        self.lineup.down()
        self.tune_current()

    def _seek(self, delta: float) -> None:
        """Skip forward (positive) or backward (negative) within the
        currently playing episode - admin mode only (see _channel_up/
        _channel_down), a grown-up-only control the kid-facing remote never
        exposes. Shows the resulting position as a brief OSD message so the
        grown-up gets feedback even though there's no visible seek bar.
        """
        self.player.seek(delta)
        arrow = "»" if delta >= 0 else "«"  # » forward / « backward
        label = f"{arrow} {abs(delta):.0f}s"
        pos = self.player.get_time_pos()
        if pos is not None:
            minutes, seconds = divmod(max(0, int(pos)), 60)
            label += f"  ({minutes}:{seconds:02d})"
        self.overlay.show_message(label)

    def _jump_last_channel(self) -> None:
        if self.adult_mode and not self.admin_mode:
            # Adult Mode's quick way back into picking a different episode
            # (UKE-29) - repurposes the remote's Back/Last-channel button
            # rather than needing to hold Power again. Kid Mode (and
            # browsing itself, where this button isn't otherwise used) keep
            # its normal "jump to previous channel" meaning below - see the
            # module docstring.
            self._quick_reopen_episode_list()
            return
        if self._last_channel_number is None:
            return
        target = self._last_channel_number
        if not self.lineup.has_number(target):
            return
        self._remember_position()
        self._last_channel_number = self.lineup.current.number
        self.lineup.select_number(target)
        self.tune_current()

    def _quick_reopen_episode_list(self) -> None:
        """Adult Mode's shortcut (UKE-29, see module docstring): jump
        straight into the current channel's episode list without first
        reopening the full grid, so switching to a different episode is one
        button press instead of several. A no-op if the channel has no
        episodes at all (nothing to list).
        """
        channel = self.lineup.current
        if not channel.episodes:
            return
        self._pre_admin_path = self._playing_path
        self._pre_admin_pos = self.player.get_time_pos() or 0.0
        self.admin_browsing = False
        self.admin_episode_browsing = True
        self.admin_insights_viewing = False
        self._browse_continue_index = None
        self._browse_on_insights = False
        self._browse_on_adult_toggle = False
        self._scroll_y = 0
        self._browse_number = channel.number
        self._browse_episode_number = channel.number
        try:
            self._browse_episode_index = channel.episodes.index(self._playing_path)
        except ValueError:
            self._browse_episode_index = 0
        self._show_admin_grid_background()
        self._refresh_admin_panel()

    def select_channel_number(self, number: int) -> bool:
        """Tune directly to a channel number. Returns False if it doesn't exist."""
        if not self.lineup.has_number(number):
            self.overlay.show_message(f"CH {number:02d}  -  NO CHANNEL")
            return False
        if number == self.lineup.current.number:
            self._show_info()
            return True
        self._remember_position()
        self._last_channel_number = self.lineup.current.number
        self.lineup.select_number(number)
        self.tune_current()
        return True

    def tune_current(self, *, show_static: bool = True) -> None:
        """Tune into the currently selected channel."""
        channel = self.lineup.current
        self.overlay.clear_standby()

        request = channel.tune_in()
        self._pending_banner = None
        # A genuine channel change should never leave a stale pause behind,
        # in any mode.
        self.paused = False

        if request is None:
            # No episodes on this channel: show the "no signal" screen.
            self.overlay.show_channel_bug(channel.number, channel.name)
            self._show_no_signal(channel)
            self._refresh_admin_panel()
            return

        if not show_static:
            # Not a channel change (first tune / waking from standby): play now.
            self._switch_deadline = None
            self.overlay.show_channel_bug(channel.number, channel.name)
            self._play_request(request)
        elif self._transition_path is not None:
            # Transition clip (glitch/static) + preloaded episode.
            self._switch_deadline = None
            self.overlay.show_channel_bug(channel.number, channel.name)
            self._playing_path = request.path
            self.player.play_transition(
                self._transition_path,
                request.path,
                start=request.start,
                static_seconds=self.config.transition_duration,
            )
        elif self.config.bridge_seconds > 0 and self._playing_path is not None:
            # No transition effect: keep the current show playing while the next
            # channel preloads, then cut over (no frozen frame). The banner is
            # shown at the cut-over (see _maybe_commit_switch), not right now.
            self._playing_path = request.path
            self.player.preload_next(request.path, start=request.start)
            self._switch_deadline = self._clock() + self.config.bridge_seconds
            self._pending_banner = (channel.number, channel.name)
        else:
            self._switch_deadline = None
            self.overlay.show_channel_bug(channel.number, channel.name)
            self._play_request(request)

        self._refresh_admin_panel()

    def _play_request(self, request: PlayRequest) -> None:
        self._playing_path = request.path
        self.player.play(request.path, start=request.start)

    def _show_no_signal(self, channel: Channel) -> None:
        self._switch_deadline = None
        self._pending_banner = None
        self._playing_path = None
        if self._colorbars_path is not None:
            self.player.play_loop(self._colorbars_path)
        else:
            self.player.stop()
        self.overlay.show_message(
            f"CH {channel.number:02d}  {channel.name}  -  NO SIGNAL", duration=6.0
        )

    # -- volume -------------------------------------------------------------
    def _volume_up(self) -> None:
        if self.admin_browsing:
            self._move_browse_cursor(dcol=1)
            return
        if self.admin_insights_viewing:
            # Read-only screen.
            return
        self._set_volume(self.volume + self.config.volume_step, unmute=True)

    def _volume_down(self) -> None:
        if self.admin_browsing:
            self._move_browse_cursor(dcol=-1)
            return
        if self.admin_insights_viewing:
            return
        # One press below zero cleanly powers off the box (safe to unplug).
        if self.config.power_off_on_min_volume and not self.muted and self.volume <= 0:
            self._power_off()
            return
        self._set_volume(self.volume - self.config.volume_step, unmute=True)

    def _set_volume(self, value: int, *, unmute: bool = False) -> None:
        self.volume = max(0, min(100, value))
        if unmute and self.muted:
            self.muted = False
            self.player.set_mute(False)
        self.player.set_volume(self.volume)
        self.overlay.show_volume(self.volume, self.muted)

    def _power_off(self) -> None:
        """Cleanly shut the Pi down so it's safe to unplug."""
        log.info("powering off (volume floor)")
        self.powered_off = True
        self._switch_deadline = None
        self._pending_banner = None
        try:
            self.overlay.clear_all()
            self.overlay.show_message("GOODBYE", duration=0)
            self.player.stop()
        except Exception:  # noqa: BLE001
            pass
        self._run_power_off_command()
        self._running = False  # exit the main loop

    def _run_power_off_command(self) -> None:
        command = list(self.config.power_off_command)
        if not command:
            return  # disabled / test mode
        try:
            subprocess.Popen(command)
        except Exception:  # noqa: BLE001
            log.exception("power-off command failed: %s", command)

    def _toggle_mute(self) -> None:
        # In the admin view, Mute is repurposed: it confirms whatever's
        # highlighted in the show grid or episode list, otherwise it's
        # pause/play (a capability the kid-facing remote never exposes).
        # Everywhere else it behaves exactly as before.
        if self.admin_episode_browsing:
            self._confirm_episode_selection()
            return
        if self.admin_browsing:
            if self._browse_continue_index is not None:
                self._confirm_continue_selection()
            elif self._browse_on_insights:
                self._confirm_insights_selection()
            elif self._browse_on_adult_toggle:
                self._confirm_adult_toggle()
            else:
                self._confirm_show_selection()
            return
        if self.admin_insights_viewing:
            # Read-only screen - Power is the only thing that does anything
            # here (see _admin_back_from_insights).
            return
        if self.adult_mode:
            self._toggle_pause()
            return
        self.muted = not self.muted
        self.player.set_mute(self.muted)
        self.overlay.show_volume(self.volume, self.muted)

    # -- admin/developer view -------------------------------------------------
    def _toggle_admin_mode(self) -> None:
        """Long-press Power (ADMIN_TOGGLE): open/close the browse grid.
        Purely about the grid itself - Adult Mode (see the module
        docstring) is independent and is never touched here, whichever way
        this goes.
        """
        if self.admin_mode:
            self._close_admin_browsing()
        else:
            self._open_admin_browsing()

    def _open_admin_browsing(self) -> None:
        # Opening admin mode always lands on the full-screen show grid,
        # cursor starting on whatever's playing. Remember exactly where
        # we are so browsing without picking anything new can resume it.
        self.admin_browsing = True
        self.admin_episode_browsing = False
        self.admin_insights_viewing = False
        self._browse_number = self.lineup.current.number
        self._browse_episode_number = None
        self._browse_episode_index = 0
        self._browse_continue_index = None
        self._browse_on_insights = False
        self._browse_on_adult_toggle = False
        self._scroll_y = 0
        self._refresh_continue_entries()
        self._pre_admin_path = self._playing_path
        self._pre_admin_pos = self.player.get_time_pos() or 0.0
        self._sync_scroll_to_cursor()
        self._show_admin_grid_background()
        self._refresh_admin_panel()

    def _close_admin_browsing(self) -> None:
        # Go back to actually watching - Kid Mode if Adult Mode is off, or
        # the same grown-up controls as before if it's on (UKE-29; see the
        # module docstring). Unlike the old single admin_mode flag, this
        # never force-unpauses under Adult Mode - there's no reason a
        # grown-up who paused before glancing at the grid should come back
        # to it playing again.
        self.admin_browsing = False
        self.admin_episode_browsing = False
        self.admin_insights_viewing = False
        self._browse_continue_index = None
        self._browse_on_insights = False
        self._browse_on_adult_toggle = False
        if not self.adult_mode and self.paused:
            self._toggle_pause()
        self.overlay.clear_admin_panel()
        if self._pre_admin_path is not None:
            # Nothing new was picked this session - resume exactly where
            # we left off rather than restarting/re-shuffling.
            self._play_request(PlayRequest(path=self._pre_admin_path, start=self._pre_admin_pos))
            self._pre_admin_path = None
        self._flash_channel_banner()

    def _show_admin_grid_background(self) -> None:
        """Swap the player onto the current viewport crop of the pre-
        composed poster-grid image (see nostalgiabox.thumbnails) so real
        show art fills the whole screen behind the highlight ring/labels
        overlay.show_admin_browser draws. Bypasses the usual 4:3 pillarbox
        filter (``use_frame_filter=False``, UKE-29) - the admin UI is a
        modern full-width screen, not the nostalgic 4:3 "tube" every actual
        show plays in (see thumbnails.py's module docstring).

        If the background image hasn't been generated yet (``--check`` was
        never run since adding shows), this is a no-op - the browse overlay
        still works, just without posters, drawn over whatever was already
        playing.
        """
        from . import thumbnails

        image_path = self._admin_thumbs_cache_dir() / thumbnails.GRID_FILENAME
        if not image_path.is_file():
            log.info("admin show-grid image not found (run `nostalgiabox --check`)")
            return

        self._scroll_y = thumbnails.clamp_scroll(self._admin_tiles(), self._scroll_y)
        viewport_path = self._admin_thumbs_cache_dir() / thumbnails.VIEWPORT_FILENAME
        cropped = thumbnails.crop_viewport(image_path, self._scroll_y, viewport_path)
        self.player.play_loop(cropped or image_path, use_frame_filter=False)

    def _sync_admin_scroll(self, *, target_top: int, target_bottom: int) -> None:
        """Scroll the grid, if needed, so the row spanning image-space rows
        ``[target_top, target_bottom)`` (see
        :func:`nostalgiabox.thumbnails.admin_section_layout`/
        ``sections_bottom``) is fully visible, then reloads the background
        image at the new scroll position (see _show_admin_grid_background).
        A no-op if the row's already fully on screen - called any time the
        browse cursor moves to a different row (see _move_browse_cursor).
        """
        from . import thumbnails

        body_h = thumbnails.body_viewport_height()
        visible_top = thumbnails.GRID_HEADER_H + self._scroll_y
        visible_bottom = visible_top + body_h
        new_scroll = self._scroll_y
        if target_top < visible_top:
            new_scroll = target_top - thumbnails.GRID_HEADER_H
        elif target_bottom > visible_bottom:
            new_scroll = target_bottom - body_h - thumbnails.GRID_HEADER_H
        new_scroll = thumbnails.clamp_scroll(self._admin_tiles(), new_scroll)
        if new_scroll != self._scroll_y:
            self._scroll_y = new_scroll
            self._show_admin_grid_background()

    def _reopen_player(self) -> None:
        """Rebuild mpv via the injected factory (real hardware only - see
        __init__); with no factory (dry-run / most tests) this is a no-op,
        the same MockPlayer instance is reused throughout. Only ever needed
        after a game hands the display back (see _launch_game) - browsing
        the admin grid/episode list/Insights never closes mpv at all.
        """
        if self._player_factory is None:
            return
        if self._drm_handoff_delay > 0:
            time.sleep(self._drm_handoff_delay)
        self.player = self._player_factory()
        self.player.on_end = self._ended.put
        self.player.set_volume(self.volume)
        self.player.set_mute(self.muted)
        # self.overlay was built once around the original player (see
        # __init__) - without repointing it here it would keep silently
        # driving the old, terminated mpv instance forever after the first
        # reopen (see OverlayManager.rebind_player's docstring for why this
        # doesn't crash, just goes quietly dead).
        self.overlay.rebind_player(self.player)

    def _refresh_admin_panel(self) -> None:
        # Once none of the three browse screens are open (UKE-29), there's
        # nothing persistent to show any more - explicitly clear rather than
        # leaving whichever screen was open last still on the overlay slot.
        # See show_adult_mode_status/show_message for Adult Mode's transient
        # feedback instead.
        if self.admin_episode_browsing:
            channel = self._channel_by_number(self._browse_episode_number)
            if channel is not None:
                self.overlay.show_admin_episode_list(
                    channel, highlight_index=self._browse_episode_index, watch_state=self.watch_state
                )
            return
        if self.admin_insights_viewing:
            summary, suggestions = self._current_insights()
            self.overlay.show_admin_insights(summary, suggestions=suggestions)
            return
        if self.admin_browsing:
            self.overlay.show_admin_browser(
                self._admin_tiles(),
                highlight_number=self._browse_number,
                continue_entries=self._continue_entries,
                continue_index=self._browse_continue_index,
                insights_selected=self._browse_on_insights,
                adult_mode=self.adult_mode,
                adult_toggle_selected=self._browse_on_adult_toggle,
                scroll_y=self._scroll_y,
            )
            return
        self.overlay.clear_admin_panel()

    def _refresh_continue_entries(self) -> None:
        """Recompute the Continue Watching row's contents (see
        watch_state.continue_watching) - cheap (in-memory dict lookups, see
        that function's docstring), so this is called any time the browse
        grid is (re)entered rather than kept incrementally up to date.
        """
        self._continue_entries = continue_watching(list(self.lineup), self.watch_state, limit=3)

    def _current_insights(self) -> tuple:
        """The Insights screen's data (UKE-29): per-channel watch stats
        rolled up fresh (see watch_state.insights_summary for why that's
        cheap enough not to bother caching), plus text-only similar-show
        suggestions for the favorite (see recommendations.py).
        """
        from .recommendations import suggest_similar

        summary = insights_summary(self._admin_tiles(), self.watch_state)
        suggestions: List[str] = []
        if summary.favorite is not None:
            suggestions = suggest_similar(summary.favorite.name)
        return summary, suggestions

    def _admin_tiles(self) -> List[Channel]:
        """Every tile the admin browse grid shows: real channels, then game
        systems, in that order. Games are never part of self.lineup (see
        __init__) - this combined view exists only for the admin browse
        screens.
        """
        return list(self.lineup) + list(self.games)

    def _channel_by_number(self, number: Optional[int]) -> Optional[Channel]:
        if number is None:
            return None
        return next((c for c in self._admin_tiles() if c.number == number), None)

    def _admin_thumbs_cache_dir(self) -> Path:
        from .thumbnails import THUMBS_SUBDIR

        return self._assets_dir / THUMBS_SUBDIR

    def _section_tiles(self, key: str) -> List[Channel]:
        """The channels/game systems in one named browse row. 'continue'
        isn't a real Channel list (see self._continue_entries instead) -
        callers asking for it get nothing back on purpose.
        """
        if key == "shows":
            return [c for c in self.lineup]
        if key == "games":
            return list(self.games)
        return []

    def _section_keys(self) -> List[str]:
        """Every browse row currently on screen, top to bottom: Continue
        Watching (only if it has anything in it), then Shows, then Games -
        each only present if it isn't empty - then the two evergreen rows,
        Insights and the Adult Mode toggle (UKE-29), always present at the
        bottom regardless of what's been watched yet. This is recomputed on
        every move rather than cached, since it's cheap and always needs to
        reflect self._continue_entries's current contents anyway.
        """
        keys = []
        if self._continue_entries:
            keys.append("continue")
        if self._section_tiles("shows"):
            keys.append("shows")
        if self._section_tiles("games"):
            keys.append("games")
        keys.append("insights")
        keys.append("adult_toggle")
        return keys

    def _current_section(self) -> str:
        if self._browse_continue_index is not None:
            return "continue"
        if self._browse_on_insights:
            return "insights"
        if self._browse_on_adult_toggle:
            return "adult_toggle"
        channel = self._channel_by_number(self._browse_number)
        if channel is not None and channel.config.kind == "game":
            return "games"
        return "shows"

    def _enter_section(self, key: str) -> None:
        """Land the cursor on a row's first tile - used when moving between
        rows (see _move_browse_cursor). Simpler than trying to preserve a
        horizontal position across rows of different lengths, and matches
        landing on the leftmost card the way most on-screen row UIs do.
        """
        self._browse_continue_index = None
        self._browse_on_insights = False
        self._browse_on_adult_toggle = False
        if key == "continue":
            self._browse_continue_index = 0
            return
        if key == "insights":
            self._browse_on_insights = True
            return
        if key == "adult_toggle":
            self._browse_on_adult_toggle = True
            return
        tiles = self._section_tiles(key)
        if tiles:
            self._browse_number = tiles[0].number

    def _section_scroll_bounds(self, key: str) -> Optional[tuple[int, int]]:
        """Image-space (top, bottom) a given row occupies, for
        _sync_admin_scroll - ``None`` for rows that are either pinned (never
        need scrolling, like Continue Watching) or don't currently exist.
        """
        from . import thumbnails

        if key == "continue":
            return None  # pinned above the header - always visible, see GRID_HEADER_H
        if key in ("shows", "games"):
            return thumbnails.section_bounds(self._admin_tiles(), "Shows" if key == "shows" else "Games")
        bottom = thumbnails.sections_bottom(self._admin_tiles())
        insights_top = bottom + thumbnails.EVERGREEN_GAP_ABOVE
        insights_bottom = insights_top + thumbnails.EVERGREEN_ROW_H
        if key == "insights":
            return insights_top, insights_bottom
        if key == "adult_toggle":
            adult_top = insights_bottom + thumbnails.EVERGREEN_GAP_ABOVE
            return adult_top, adult_top + thumbnails.EVERGREEN_ROW_H
        return None

    def _move_browse_cursor(self, *, drow: int = 0, dcol: int = 0) -> None:
        """Move the browse cursor. The screen is a stack of independent
        single-row "swimlanes" - Continue Watching, Shows, Games, Insights,
        Adult Mode (each only present if it has anything in it, except the
        last two which are always there; see _section_keys) - rather than a
        flat grid. Channel Up/Down move between rows and stop at the top/
        bottom (no wraparound between rows); Volume Up/Down move within
        whichever row the cursor is on, wrapping at its ends.
        """
        sections = self._section_keys()
        if not sections:
            return
        current_key = self._current_section()
        if current_key not in sections:
            current_key = sections[0]
            self._enter_section(current_key)

        if dcol:
            if current_key == "continue":
                n = len(self._continue_entries)
                if n:
                    self._browse_continue_index = (self._browse_continue_index + dcol) % n
            elif current_key not in ("insights", "adult_toggle"):
                numbers = [c.number for c in self._section_tiles(current_key)]
                if numbers:
                    pos = numbers.index(self._browse_number) if self._browse_number in numbers else 0
                    self._browse_number = numbers[(pos + dcol) % len(numbers)]
                    # A section can wrap onto multiple visual rows (UKE-29,
                    # see thumbnails.admin_section_layout) - moving within it
                    # via Volume Up/Down can land on a tile in a different
                    # row than the one currently on screen, same as moving
                    # between sections entirely (below) can.
                    self._sync_scroll_to_cursor()
            self._refresh_admin_panel()
            return

        if drow:
            idx = sections.index(current_key)
            new_idx = idx + drow
            if 0 <= new_idx < len(sections):
                self._enter_section(sections[new_idx])
                self._sync_scroll_to_cursor()
            self._refresh_admin_panel()

    def _sync_scroll_to_cursor(self) -> None:
        """Scroll the grid, if needed, so whatever's currently highlighted -
        a tile, or one of the two evergreen rows - is fully visible (UKE-29,
        see _sync_admin_scroll). A no-op for the Continue Watching row
        (pinned above the header - see GRID_HEADER_H - so it never needs
        scrolling) or if nothing's actually highlighted yet.
        """
        from . import thumbnails

        key = self._current_section()
        if key == "continue":
            return
        if key in ("insights", "adult_toggle"):
            bounds = self._section_scroll_bounds(key)
        else:
            bounds = None
            channel = self._channel_by_number(self._browse_number)
            if channel is not None:
                for section in thumbnails.admin_section_layout(self._admin_tiles()):
                    for tile in section.tiles:
                        if tile.channel.number == channel.number:
                            bounds = thumbnails.tile_bounds(tile)
                            break
                    if bounds is not None:
                        break
        if bounds is not None:
            self._sync_admin_scroll(target_top=bounds[0], target_bottom=bounds[1])

    def _confirm_show_selection(self) -> None:
        self.admin_browsing = False
        channel = self._channel_by_number(self._browse_number)
        if channel is not None:
            self.admin_episode_browsing = True
            self._browse_episode_number = channel.number
            self._browse_episode_index = 0
        self._refresh_admin_panel()

    def _confirm_adult_toggle(self) -> None:
        """Flip Adult Mode on/off (UKE-29, see the module docstring) - stays
        on the grid (unlike confirming a show/Insights) since there's
        nothing to navigate into, just a brief transient confirmation.
        """
        self.adult_mode = not self.adult_mode
        self.overlay.show_adult_mode_status(on=self.adult_mode)
        self._refresh_admin_panel()

    def _confirm_continue_selection(self) -> None:
        """Resume the highlighted Continue Watching entry exactly where it
        was left off - unlike picking a channel (which drills into its
        episode list), this plays immediately, the way clicking a Netflix
        continue-watching tile does.
        """
        index = self._browse_continue_index
        if index is None or not (0 <= index < len(self._continue_entries)):
            return
        entry = self._continue_entries[index]
        self.admin_browsing = False
        self._browse_continue_index = None
        self._play_specific_episode(entry.channel_number, entry.episode_path, start=entry.resume_position)
        self._pre_admin_path = None  # something new is playing; nothing to resume
        self._browse_number = self.lineup.current.number
        self._refresh_admin_panel()

    def _confirm_insights_selection(self) -> None:
        """Open the Insights view (UKE-29) - a passive read-only screen, so
        unlike confirming a show/continue entry there's nothing to play and
        no mpv/player interaction at all here.
        """
        self.admin_browsing = False
        self.admin_insights_viewing = True
        self._refresh_admin_panel()

    def _admin_back_to_shows(self) -> None:
        self.admin_episode_browsing = False
        self.admin_browsing = True
        self._browse_continue_index = None
        self._refresh_continue_entries()
        self._refresh_admin_panel()

    def _admin_back_from_insights(self) -> None:
        self.admin_insights_viewing = False
        self.admin_browsing = True
        self._browse_on_insights = True  # land back on the Insights tile, not the grid's start
        self._refresh_admin_panel()

    def _move_episode_cursor(self, delta: int) -> None:
        channel = self._channel_by_number(self._browse_episode_number)
        if channel is None or not channel.episodes:
            return
        n = len(channel.episodes)
        self._browse_episode_index = (self._browse_episode_index + delta) % n
        self._refresh_admin_panel()

    def _confirm_episode_selection(self) -> None:
        channel = self._channel_by_number(self._browse_episode_number)
        has_selection = channel is not None and 0 <= self._browse_episode_index < len(channel.episodes)

        if has_selection and channel.config.kind == "game":
            # Games hand off to RetroArch and land back on this exact game
            # list - nothing "starts playing" in the mpv sense, so unlike a
            # show episode, admin_episode_browsing is left untouched (see
            # _launch_game).
            self._launch_game(channel, channel.episodes[self._browse_episode_index])
            self._refresh_admin_panel()
            return

        self.admin_episode_browsing = False
        if has_selection:
            episode_path = channel.episodes[self._browse_episode_index]
            self._play_specific_episode(channel.number, episode_path)
            self._pre_admin_path = None  # something new is playing; nothing to resume
        self._browse_number = self.lineup.current.number
        self._refresh_admin_panel()

    def _launch_game(self, channel: Channel, rom_path: Path) -> None:
        """Hand the display to RetroArch for one game, then return to
        exactly the admin browse screen the game was launched from.

        This is the one remaining place mpv actually closes (see the module
        docstring) - confirmed safe on real hardware by
        scripts/spike_mpv_retroarch_handoff.py (UKE-28): closing mpv
        releases the DRM master fast enough for RetroArch to get a picture,
        and a fresh MpvPlayer reopens cleanly afterward - repeatedly, not
        just once.
        """
        core = channel.config.core
        if not core:
            log.warning("game channel %r has no core configured; not launching", channel.name)
            return
        self.player.close()
        if self._drm_handoff_delay > 0:
            time.sleep(self._drm_handoff_delay)
        try:
            self._game_launcher(core, rom_path)
        finally:
            if self.watch_state is not None:
                # Boolean played/play-count only (UKE-29) - RetroArch gives
                # no duration or position signal, so "played" is the honest
                # v1 semantic regardless of exit code.
                self.watch_state.record_game_played(channel.number, channel.config.path, rom_path)
            # Back to browsing: reopen mpv and re-arm the poster-grid loop
            # image (the same thing _toggle_admin_mode does on entry) so the
            # episode-list overlay has its usual backdrop again.
            self._reopen_player()
            self._show_admin_grid_background()

    def _play_specific_episode(self, channel_number: int, episode_path: Path, *, start: float = 0.0) -> None:
        """Play exactly this episode (picked from the admin episode list, or
        resumed from the Continue Watching row via ``start``), bypassing the
        channel's normal tune-in behaviour (random/resume/broadcast) for
        this one play-through.
        """
        if not self.lineup.has_number(channel_number):
            return
        self._remember_position()
        self._last_channel_number = self.lineup.current.number
        self.lineup.select_number(channel_number)
        channel = self.lineup.current
        self.overlay.clear_standby()
        self._pending_banner = None
        self._switch_deadline = None
        self.overlay.show_channel_bug(channel.number, channel.name)
        self._play_request(PlayRequest(path=episode_path, start=start))

    def _toggle_pause(self) -> None:
        self.paused = not self.paused
        self.player.set_pause(self.paused)
        # No persistent overlay to refresh anymore while just watching under
        # Adult Mode (UKE-29) - a brief OSD message instead, same treatment
        # as every other Adult Mode action (seek, subtitles).
        self.overlay.show_message("Paused" if self.paused else "Playing")
        self._refresh_admin_panel()

    # -- info / standby -----------------------------------------------------
    def _show_info(self) -> None:
        # Adult Mode repurposes Info into a subtitle toggle while actually
        # watching (not browsing) - UKE-29, see the module docstring. Kid
        # Mode (and browsing itself, which has its own Info-less footer
        # hints) keep Info's original channel-banner behaviour. Internal
        # callers that specifically want the banner regardless of mode (e.g.
        # closing the grid) use _flash_channel_banner directly instead.
        if self.adult_mode and not self.admin_mode:
            self._toggle_subtitles()
            return
        self._flash_channel_banner()

    def _flash_channel_banner(self) -> None:
        channel = self.lineup.current
        self.overlay.show_channel_bug(channel.number, channel.name)

    def _toggle_subtitles(self) -> None:
        self.subtitles_visible = not self.subtitles_visible
        self.player.set_subtitle_visible(self.subtitles_visible)
        self.overlay.show_message("Subtitles: ON" if self.subtitles_visible else "Subtitles: OFF")

    def _toggle_standby(self) -> None:
        self.standby = not self.standby
        if self.standby:
            self._remember_position()
            self._switch_deadline = None
            self._pending_banner = None
            # Standby is the kid-proof reset point: never leave the box
            # sitting in admin/Adult Mode / paused / mid-browse underneath a
            # blanked screen (UKE-29 - this now also drops Adult Mode, which
            # otherwise deliberately survives everything else).
            self.admin_browsing = False
            self.admin_episode_browsing = False
            self.admin_insights_viewing = False
            self.adult_mode = False
            self._browse_number = None
            self._browse_episode_number = None
            self._browse_episode_index = 0
            self._browse_continue_index = None
            self._browse_on_insights = False
            self._browse_on_adult_toggle = False
            self._scroll_y = 0
            self._continue_entries = []
            self._pre_admin_path = None
            self.paused = False
            self.player.stop()
            self.overlay.clear_all()
            self.overlay.show_standby()
        else:
            self.overlay.clear_standby()
            self.tune_current(show_static=False)

    # -- direct channel entry ----------------------------------------------
    def _push_digit(self, digit: int) -> None:
        self._digit_buffer = (self._digit_buffer + str(digit))[-3:]
        self._digit_deadline = self._clock() + self._digit_entry_timeout
        self.overlay.show_message(f"CH {self._digit_buffer}_", duration=self._digit_entry_timeout)

    def _confirm_digits(self) -> None:
        if not self._digit_buffer:
            return
        number = int(self._digit_buffer)
        self._digit_buffer = ""
        self._digit_deadline = 0.0
        self.select_channel_number(number)

    def _maybe_commit_digits(self, now: float) -> None:
        if self._digit_buffer and now >= self._digit_deadline:
            self._confirm_digits()

    # -- playback-finished handling ----------------------------------------
    def _drain_playback_events(self) -> None:
        advanced = False
        while True:
            try:
                reason = self._ended.get_nowait()
            except queue.Empty:
                break
            # Coalesce: only advance once even if several events queued up.
            if reason in (END_EOF, END_ERROR) and not advanced and not self.standby:
                if reason == END_EOF:
                    self._mark_current_watched()
                self._advance_current()
                advanced = True

    def _advance_current(self) -> None:
        request = self.lineup.current.advance()
        if request is None:
            self._show_no_signal(self.lineup.current)
        else:
            self._play_request(request)

    # -- helpers ------------------------------------------------------------
    def _remember_position(self) -> None:
        # Watch-state tracking (UKE-29) runs regardless of tune_in mode -
        # unlike the "resume" feature below, watched/continue-watching state
        # isn't something that should depend on how channel-surfing itself
        # behaves.
        self._record_watch_progress()
        if self.config.tune_in != "resume" or self._playing_path is None:
            return
        pos = self.player.get_time_pos()
        if pos is not None:
            self.lineup.current.remember(self._playing_path, pos)

    def _record_watch_progress(self) -> None:
        """Save how far into the current episode we are (UKE-29). Probing a
        file's duration is a real subprocess call (ffprobe), so it's only
        done once per episode - once known, it's cached in the watch-state
        record itself and reused on every subsequent call, so this never
        adds a hitch to a channel change the way probing on every call would.
        """
        if self.watch_state is None or self._playing_path is None:
            return
        pos = self.player.get_time_pos()
        if pos is None:
            return
        channel = self.lineup.current
        existing = self.watch_state.episode_state(channel.number, channel.config.path, self._playing_path)
        duration = existing.duration
        if not duration:
            from .probe import probe_duration

            duration = probe_duration(self._playing_path) or 0.0
        self.watch_state.record_episode_position(
            channel.number, channel.config.path, self._playing_path, pos, duration
        )

    def _mark_current_watched(self) -> None:
        """Mark the currently-playing episode watched outright - called on a
        natural end-of-file, which is unambiguous regardless of whatever
        position was last sampled.
        """
        if self.watch_state is None or self._playing_path is None:
            return
        channel = self.lineup.current
        self.watch_state.mark_episode_watched(channel.number, channel.config.path, self._playing_path)

    def _select_start_channel(self) -> None:
        if self.config.start_channel is not None and self.lineup.has_number(
            self.config.start_channel
        ):
            self.lineup.select_number(self.config.start_channel)

    def _resolve_asset(self, filename: str) -> Optional[Path]:
        path = self._assets_dir / filename
        return path if path.is_file() else None

    def _resolve_transition_asset(self) -> Optional[Path]:
        effect = self.config.transition_effect
        if effect == "none":
            return None
        filename = GLITCH_FILENAME if effect == "glitch" else STATIC_FILENAME
        return self._resolve_asset(filename)


def _suppress_console_echo() -> None:
    """On real hardware, stop the kernel VT from echoing raw keystrokes onto
    the screen (UKE-29).

    All real input comes through evdev (see input/keyboard.py) - nothing
    reads stdin - but the service's stdin is still wired to the physical
    console (``StandardInput=tty`` + ``TTYPath=/dev/tty1`` in
    nostalgiabox.service, needed so the process owns that VT for DRM/KMS).
    A VT's own keyboard-to-tty line discipline keeps echoing typed
    characters to the screen regardless of that - it's a completely
    separate path from evdev, so evdev's own ``keyboard_grab`` option has no
    effect on it. Confirmed on real hardware: every physical keypress
    briefly flashed a literal "a" (etc.) on the TV - not garbled input, just
    the console dutifully echoing what it was told to type.

    Best-effort and permanent for the life of the process: TTYReset=yes in
    the service unit restores the terminal when it stops, so there's
    nothing to undo here on our end.
    """
    try:
        import termios

        if not sys.stdin.isatty():
            return
        fd = sys.stdin.fileno()
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~(termios.ECHO | termios.ICANON)  # lflags
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except Exception:  # noqa: BLE001 - cosmetic; never worth failing startup over
        log.debug("could not suppress console echo", exc_info=True)


def run_from_config(config: Config, *, dry_run: bool = False) -> None:
    """Convenience entry point used by the CLI."""
    if not dry_run:
        _suppress_console_echo()
    app = TVApp.from_config(config, dry_run=dry_run)
    app.run()


__all__ = ["TVApp", "run_from_config"]
