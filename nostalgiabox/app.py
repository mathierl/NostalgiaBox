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
"""

from __future__ import annotations

import logging
import queue
import subprocess
import time
from pathlib import Path
from typing import Callable, List, Optional

from .actions import Action, InputEvent
from .admin_server import AdminServer
from .channel import (
    Channel,
    ChannelLineup,
    PlayRequest,
    build_game_channels,
    build_lineup,
    detect_season,
    episode_title,
    item_label,
)
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
from .watch_state import STATE_FILENAME, STATE_SUBDIR, ContinueEntry, WatchState, continue_watching

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


class _NullAdminUiProcess:
    """Stand-in "process handle" for when nothing real was launched (dry-run
    / most tests - see _noop_admin_ui_launcher). Duck-types the bit of
    subprocess.Popen's interface TVApp actually uses.
    """

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def wait(self, timeout: Optional[float] = None) -> int:
        return 0


def _noop_admin_ui_launcher(url: str) -> _NullAdminUiProcess:
    """Default admin_ui_launcher when TVApp is built directly (dry-run, and
    every test that doesn't specifically care about the browser handoff -
    which, unlike game launches, is *not* an opt-in action a test can just
    avoid triggering: entering admin mode at all goes through this). Returns
    a harmless stand-in instead of actually spawning a browser - from_config
    wires up _default_admin_ui_launcher for real hardware.
    """
    return _NullAdminUiProcess()


def _default_admin_ui_launcher(url: str) -> subprocess.Popen:
    """Launch Chromium in kiosk mode against the admin server, non-blocking:
    unlike _default_game_launcher (which blocks, because RetroArch
    legitimately owns the whole box until it exits), the main loop has to
    keep running while the browser is up - it's the thing feeding /state
    updates as the cursor moves (see admin_server.py's module docstring).

    --ozone-platform=drm is Chromium's own native DRM/KMS backend: this Pi
    has no X11/Wayland session at all (mpv talks DRM/KMS directly), so a
    normal desktop Chromium build has no window server to hand it a window.
    See scripts/spike_mpv_chromium_handoff.py, which exists specifically to
    validate this on real hardware before this default is trusted.
    """
    import shutil

    chromium = shutil.which("chromium-browser") or shutil.which("chromium") or "chromium"
    cmd = [
        chromium,
        "--ozone-platform=drm",
        "--enable-features=UseOzonePlatform",
        "--kiosk",
        "--noerrdialogs",
        "--disable-infobars",
        "--disable-session-crashed-bubble",
        "--incognito",
        url,
    ]
    return subprocess.Popen(cmd)


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
        admin_ui_launcher: Optional[Callable[[str], object]] = None,
        admin_server: Optional["AdminServer"] = None,
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
        # Browser-based admin UI (UKE-29): admin_server is the local HTTP
        # server the browser polls (None by default - as in most tests -
        # means "don't run a real server"; from_config wires up a real one).
        # admin_ui_launcher defaults to a harmless no-op rather than
        # _default_admin_ui_launcher the way game_launcher does, because
        # entering admin mode at all - unlike launching a game - isn't
        # something most tests can just avoid triggering.
        self._admin_server = admin_server
        self._admin_ui_launcher = admin_ui_launcher or _noop_admin_ui_launcher
        self._admin_ui_process: Optional[object] = None

        self.lineup: ChannelLineup = build_lineup(config)
        # Game systems (see UKE-28): shown in the admin browse grid alongside
        # real channels (_admin_tiles), but never part of self.lineup - the
        # kid-facing tuner must never be able to land on one.
        self.games: List[Channel] = build_game_channels(config)

        # Runtime state.
        self.volume = config.initial_volume
        self.muted = False
        self.standby = False
        self.powered_off = False
        # Admin/developer view: hidden behind a long power-button hold (see
        # input/keyboard.py). Grown-ups get an overview of every channel and
        # pause/play; the kid-facing remote is unaffected either way.
        self.admin_mode = False
        self.paused = False
        # Admin mode has two nested browse screens. admin_browsing = True is
        # the top-level "select a channel" poster grid: Channel Up/Down move a
        # row cursor, Volume Up/Down (repurposed here only) move a column
        # cursor, Mute confirms and drills into that channel's episode list
        # (admin_episode_browsing = True): Channel Up/Down move an episode
        # cursor, Mute plays that exact episode, Power backs out to the show
        # grid instead of standby. Long-press Power always exits admin mode
        # entirely, from either screen.
        self.admin_browsing = False
        self.admin_episode_browsing = False
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

        app = cls(
            config,
            player,
            input_manager,
            assets_dir=assets_dir,
            player_factory=player_factory,
            watch_state=watch_state,
            admin_ui_launcher=_default_admin_ui_launcher,
        )
        # Attached after construction since the server's state_provider is a
        # bound method of app itself - only actually called once the browser
        # is up and polling, by which point app fully exists either way.
        app._admin_server = AdminServer(
            html_path=Path(__file__).resolve().parent / "admin_ui" / "index.html",
            poster_dir=app._admin_thumbs_cache_dir(),
            state_provider=app._admin_state_snapshot,
        )
        return app

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> None:
        """Power on: set volume, start input, and tune to the first channel."""
        self.player.set_volume(self.volume)
        self.player.set_mute(self.muted)
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
        self._remember_position()
        self._last_channel_number = self.lineup.current.number
        self.lineup.down()
        self.tune_current()

    def _jump_last_channel(self) -> None:
        if self._last_channel_number is None:
            return
        target = self._last_channel_number
        if not self.lineup.has_number(target):
            return
        self._remember_position()
        self._last_channel_number = self.lineup.current.number
        self.lineup.select_number(target)
        self.tune_current()

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
        if self.admin_mode:
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
        self._set_volume(self.volume + self.config.volume_step, unmute=True)

    def _volume_down(self) -> None:
        if self.admin_browsing:
            self._move_browse_cursor(dcol=-1)
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
            else:
                self._confirm_show_selection()
            return
        if self.admin_mode:
            self._toggle_pause()
            return
        self.muted = not self.muted
        self.player.set_mute(self.muted)
        self.overlay.show_volume(self.volume, self.muted)

    # -- admin/developer view -------------------------------------------------
    def _toggle_admin_mode(self) -> None:
        self.admin_mode = not self.admin_mode
        if self.admin_mode:
            # Opening admin mode always lands on the full-screen show grid,
            # cursor starting on whatever's playing. Remember exactly where
            # we are so browsing without picking anything new can resume it.
            self.admin_browsing = True
            self.admin_episode_browsing = False
            self._browse_number = self.lineup.current.number
            self._browse_episode_number = None
            self._browse_episode_index = 0
            self._browse_continue_index = None
            self._refresh_continue_entries()
            self._pre_admin_path = self._playing_path
            self._pre_admin_pos = self.player.get_time_pos() or 0.0
            self._open_admin_ui()
        else:
            self.admin_browsing = False
            self.admin_episode_browsing = False
            self._browse_continue_index = None
            if self.paused:
                self._toggle_pause()
            self.overlay.clear_admin_panel()
            self._close_admin_ui()
            self._reopen_player()
            if self._pre_admin_path is not None:
                # Nothing new was picked this session - resume exactly where
                # we left off rather than restarting/re-shuffling.
                self._play_request(PlayRequest(path=self._pre_admin_path, start=self._pre_admin_pos))
                self._pre_admin_path = None
            self._show_info()

    def _open_admin_ui(self) -> None:
        """Close mpv and hand the display to the browser-based admin UI (see
        admin_server.py): the grid and episode-list screens are rendered
        there now, not as ASS overlays - see UKE-29. Non-blocking (the
        launcher is expected to return immediately, e.g. via
        subprocess.Popen) since, unlike a game launch, the main loop has to
        keep running while the browser is up: it's what actually moves the
        cursor the browser is polling for (see TVApp._admin_state_snapshot).

        Falls back to reopening the normal picture if the browser fails to
        start at all, rather than leaving the box on a dead black screen.
        """
        if self._admin_server is not None:
            self._admin_server.start()  # idempotent - no-op if already running
        url = self._admin_server.url if self._admin_server is not None else "about:blank"
        self.player.close()
        try:
            process = self._admin_ui_launcher(url)
        except Exception:  # noqa: BLE001
            log.exception("failed to launch admin UI")
            process = None
        if process is None:
            log.warning("admin UI failed to start - falling back to the normal picture")
            self._reopen_player()
            self.admin_mode = False
            self.admin_browsing = False
            self.admin_episode_browsing = False
            if self._pre_admin_path is not None:
                self._play_request(PlayRequest(path=self._pre_admin_path, start=self._pre_admin_pos))
                self._pre_admin_path = None
            return
        self._admin_ui_process = process

    def _close_admin_ui(self) -> None:
        """Terminate the browser process, if one is open. Leaves mpv closed
        either way - callers decide what (if anything) to reopen next, since
        that differs between "admin mode is done" (a fresh mpv) and "a game
        was launched from the game list" (back to the admin UI - see
        _launch_game).
        """
        process = self._admin_ui_process
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                process.kill()
                process.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
        self._admin_ui_process = None

    def _reopen_player(self) -> None:
        """Rebuild mpv via the injected factory (real hardware only - see
        __init__); with no factory (dry-run / most tests) this is a no-op,
        the same MockPlayer instance is reused throughout.
        """
        if self._player_factory is None:
            return
        self.player = self._player_factory()
        self.player.on_end = self._ended.put
        self.player.set_volume(self.volume)
        self.player.set_mute(self.muted)

    def _refresh_admin_panel(self) -> None:
        if not self.admin_mode:
            return
        if self.admin_episode_browsing or self.admin_browsing:
            # Nothing to push - the browser polls GET /state itself (see
            # admin_server.py), which always reflects the current cursor
            # position live. Kept as a no-op call site (rather than removed)
            # so every place that used to need to "tell the overlay to
            # redraw" after a cursor move still has somewhere to do it, in
            # case a future renderer needs pushing again.
            return
        self.overlay.show_admin_panel(self.lineup, paused=self.paused)

    def _refresh_continue_entries(self) -> None:
        """Recompute the Continue Watching row's contents (see
        watch_state.continue_watching) - cheap (in-memory dict lookups, see
        that function's docstring), so this is called any time the browse
        grid is (re)entered rather than kept incrementally up to date.
        """
        self._continue_entries = continue_watching(list(self.lineup), self.watch_state, limit=3)

    # -- browser-based admin UI (see admin_server.py) -----------------------
    # Everything below is pure presentation: it reads the same cursor/section
    # state the ASS renderer above reads, just packaged as JSON instead of
    # ASS markup. Called on every GET /state poll, so it stays cheap - no
    # disk IO beyond watch_state's already-in-memory dict, no subprocess
    # calls (poster existence is a stat(), not a generation).
    def _admin_state_snapshot(self) -> dict:
        if self.admin_episode_browsing:
            return {"mode": "episode_list", "episode_list": self._episode_list_snapshot()}
        return {"mode": "grid", **self._grid_snapshot()}

    def _grid_snapshot(self) -> dict:
        continue_index = self._browse_continue_index
        continue_items = [
            {
                "index": i,
                "channel_number": entry.channel_number,
                "channel_name": entry.channel_name,
                "title": entry.title,
                "minutes_left": entry.minutes_left,
                "selected": continue_index == i,
            }
            for i, entry in enumerate(self._continue_entries)
        ]

        sections = []
        for title, key in (("Shows", "shows"), ("Games", "games")):
            tiles = self._section_tiles(key)
            if not tiles:
                continue
            sections.append({"title": title, "kind": key, "tiles": [self._tile_snapshot(c, continue_index) for c in tiles]})

        return {"continue": continue_items, "sections": sections}

    def _tile_snapshot(self, channel: Channel, continue_index: Optional[int]) -> dict:
        from .thumbnails import poster_filename

        count = len(channel.episodes)
        is_game = channel.config.kind == "game"
        watched_count: Optional[int] = None
        if self.watch_state is not None:
            if is_game:
                watched_count = sum(
                    1
                    for rom in channel.episodes
                    if self.watch_state.game_state(channel.number, channel.config.path, rom).played
                )
            else:
                watched_count = sum(
                    1
                    for ep in channel.episodes
                    if self.watch_state.episode_state(channel.number, channel.config.path, ep).watched
                )
        poster_url = None
        if channel.config.kind != "game":
            poster_path = self._admin_thumbs_cache_dir() / poster_filename(channel)
            if poster_path.is_file():
                poster_url = f"/poster/{poster_filename(channel)}"
        return {
            "number": channel.number,
            "name": channel.name,
            "kind": channel.config.kind,
            "count": count,
            "count_label": f"{count} {item_label(channel, count)}",
            "poster_url": poster_url,
            "selected": continue_index is None and channel.number == self._browse_number,
            "watched_count": watched_count,
            "all_watched": bool(watched_count is not None and count > 0 and watched_count == count),
        }

    def _episode_list_snapshot(self) -> dict:
        channel = self._channel_by_number(self._browse_episode_number)
        if channel is None:
            return {}
        episodes = []
        is_game = channel.config.kind == "game"
        for i, path in enumerate(channel.episodes):
            watched = False
            in_progress = False
            if self.watch_state is not None:
                if is_game:
                    # Games have no position/duration to be "in progress" at
                    # (see watch_state.py) - just played or not.
                    watched = self.watch_state.game_state(channel.number, channel.config.path, path).played
                else:
                    state = self.watch_state.episode_state(channel.number, channel.config.path, path)
                    watched = state.watched
                    in_progress = state.in_progress
            episodes.append(
                {
                    "index": i,
                    "title": episode_title(path),
                    "season": detect_season(str(path)),
                    "watched": watched,
                    "in_progress": in_progress,
                    "selected": i == self._browse_episode_index,
                }
            )
        count = len(channel.episodes)
        return {
            "channel_number": channel.number,
            "channel_name": channel.name,
            "kind": channel.config.kind,
            "item_label": item_label(channel, count),
            "total": count,
            "highlight_index": self._browse_episode_index,
            "episodes": episodes,
        }

    def _admin_tiles(self) -> List[Channel]:
        """Every tile the admin browse grid shows: real channels, then game
        systems, in that order. Games are never part of self.lineup - see
        __init__ - this combined view exists only for the admin browse
        screens (the small corner panel from show_admin_panel stays
        channels-only, deliberately, since it's shown while actively
        watching, not browsing).
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
        each only present if it isn't empty. This is recomputed on every
        move rather than cached, since it's cheap and always needs to
        reflect self._continue_entries's current contents anyway.
        """
        keys = []
        if self._continue_entries:
            keys.append("continue")
        if self._section_tiles("shows"):
            keys.append("shows")
        if self._section_tiles("games"):
            keys.append("games")
        return keys

    def _current_section(self) -> str:
        if self._browse_continue_index is not None:
            return "continue"
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
        if key == "continue":
            self._browse_continue_index = 0
            return
        self._browse_continue_index = None
        tiles = self._section_tiles(key)
        if tiles:
            self._browse_number = tiles[0].number

    def _move_browse_cursor(self, *, drow: int = 0, dcol: int = 0) -> None:
        """Move the browse cursor. The screen is a stack of independent
        single-row "swimlanes" - Continue Watching, Shows, Games (each only
        present if it has anything in it; see _section_keys) - rather than
        the old flat multi-row grid. Channel Up/Down move between rows and
        stop at the top/bottom (no wraparound between rows); Volume Up/Down
        move within whichever row the cursor is on, wrapping at its ends.
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
            else:
                numbers = [c.number for c in self._section_tiles(current_key)]
                if numbers:
                    pos = numbers.index(self._browse_number) if self._browse_number in numbers else 0
                    self._browse_number = numbers[(pos + dcol) % len(numbers)]
            self._refresh_admin_panel()
            return

        if drow:
            idx = sections.index(current_key)
            new_idx = idx + drow
            if 0 <= new_idx < len(sections):
                self._enter_section(sections[new_idx])
            self._refresh_admin_panel()

    def _confirm_show_selection(self) -> None:
        self.admin_browsing = False
        channel = self._channel_by_number(self._browse_number)
        if channel is not None:
            self.admin_episode_browsing = True
            self._browse_episode_number = channel.number
            self._browse_episode_index = 0
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

    def _admin_back_to_shows(self) -> None:
        self.admin_episode_browsing = False
        self.admin_browsing = True
        self._browse_continue_index = None
        self._refresh_continue_entries()
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

        The display at this point is the browser admin UI, not mpv (mpv was
        already closed when admin mode was entered - see _open_admin_ui), so
        this closes/reopens *that*, not mpv directly. The underlying mpv
        DRM-release timing this depends on was confirmed safe on real
        hardware by scripts/spike_mpv_retroarch_handoff.py (UKE-28); the
        Chromium side of this same handoff is what
        scripts/spike_mpv_chromium_handoff.py exists to validate (UKE-29).
        """
        core = channel.config.core
        if not core:
            log.warning("game channel %r has no core configured; not launching", channel.name)
            return
        self._close_admin_ui()
        try:
            self._game_launcher(core, rom_path)
        finally:
            if self.watch_state is not None:
                # Boolean played/play-count only (UKE-29) - RetroArch gives
                # no duration or position signal, so "played" is the honest
                # v1 semantic regardless of exit code.
                self.watch_state.record_game_played(channel.number, channel.config.path, rom_path)
            # Back to browsing: reopen the admin UI (the same thing
            # _toggle_admin_mode does on entry) so the game list is showing
            # again, exactly where this game was launched from.
            self._open_admin_ui()

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
        self._refresh_admin_panel()

    # -- info / standby -----------------------------------------------------
    def _show_info(self) -> None:
        channel = self.lineup.current
        self.overlay.show_channel_bug(channel.number, channel.name)

    def _toggle_standby(self) -> None:
        self.standby = not self.standby
        if self.standby:
            self._remember_position()
            self._switch_deadline = None
            self._pending_banner = None
            # Standby is the kid-proof reset point: never leave the box
            # sitting in admin mode / paused / mid-browse underneath a
            # blanked screen.
            self.admin_mode = False
            self.admin_browsing = False
            self.admin_episode_browsing = False
            self._browse_number = None
            self._browse_episode_number = None
            self._browse_episode_index = 0
            self._browse_continue_index = None
            self._continue_entries = []
            self._pre_admin_path = None
            self.paused = False
            if self._admin_ui_process is not None:
                # The admin UI (browser) was up, not mpv - close it and get
                # a live player back before touching it below.
                self._close_admin_ui()
                self._reopen_player()
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


def run_from_config(config: Config, *, dry_run: bool = False) -> None:
    """Convenience entry point used by the CLI."""
    app = TVApp.from_config(config, dry_run=dry_run)
    app.run()


__all__ = ["TVApp", "run_from_config"]
