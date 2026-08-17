import pytest

from nostalgiabox.actions import Action, InputEvent
from nostalgiabox.app import TVApp
from nostalgiabox.config import config_from_dict
from nostalgiabox.input.manager import InputManager
from nostalgiabox.player import END_EOF, MockPlayer
from nostalgiabox.watch_state import WatchState
from tests.helpers import FakeClock, make_show


def build_app(
    tmp_path,
    *,
    assets_dir=None,
    game_launcher=None,
    player_factory=None,
    watch_state=None,
    drm_handoff_delay=0.0,
    **overrides,
):
    for name in ("dragon", "arthur", "rugrats"):
        make_show(tmp_path, name, 4)
    data = {
        "shuffle_seed": 7,
        "start_channel": 2,
        "start_offset": 0,  # keep test assertions on start=0 unless overridden
        "power_off_command": [],  # no-op in tests (never actually shut down)
        "channels": [
            {"number": 2, "name": "Dragon Tales", "path": str(tmp_path / "dragon")},
            {"number": 3, "name": "Arthur", "path": str(tmp_path / "arthur")},
            {"number": 4, "name": "Rugrats", "path": str(tmp_path / "rugrats")},
        ],
    }
    data.update(overrides)
    config = config_from_dict(data)
    clock = FakeClock()
    player = MockPlayer()
    app = TVApp(
        config,
        player,
        InputManager([]),
        clock=clock,
        assets_dir=assets_dir,
        game_launcher=game_launcher,
        player_factory=player_factory,
        watch_state=watch_state,
        drm_handoff_delay=drm_handoff_delay,
    )
    return app, player, clock


def send(app, action, value=None):
    app.handle_event(InputEvent(action, value))


def test_start_tunes_to_start_channel_and_plays(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    assert app.lineup.current.number == 2
    assert player.current is not None  # an episode is playing
    assert player.volume == 70
    assert player.overlays.get(1) and "Dragon Tales" in player.overlays[1]


def test_channel_up_down_wraps(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.CHANNEL_UP)
    assert app.lineup.current.number == 3
    send(app, Action.CHANNEL_UP)
    assert app.lineup.current.number == 4
    send(app, Action.CHANNEL_UP)
    assert app.lineup.current.number == 2  # wrapped
    send(app, Action.CHANNEL_DOWN)
    assert app.lineup.current.number == 4  # wrapped back


def test_volume_controls(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.VOLUME_UP)
    assert app.volume == 75 and player.volume == 75
    send(app, Action.VOLUME_DOWN)
    assert app.volume == 70
    # volume overlay was drawn
    assert "Volume" in player.overlays[2]


def test_volume_clamps(tmp_path):
    app, player, _ = build_app(tmp_path, initial_volume=98, volume_step=5)
    app.start()
    send(app, Action.VOLUME_UP)
    assert app.volume == 100
    for _ in range(30):
        send(app, Action.VOLUME_DOWN)
    assert app.volume == 0


def test_volume_down_at_zero_powers_off(tmp_path):
    app, player, _ = build_app(tmp_path, initial_volume=10, volume_step=5)
    app.start()
    send(app, Action.VOLUME_DOWN)   # 10 -> 5
    send(app, Action.VOLUME_DOWN)   # 5 -> 0
    assert app.volume == 0 and not app.powered_off
    send(app, Action.VOLUME_DOWN)   # one more at 0 -> power off
    assert app.powered_off is True
    assert app._running is False
    assert player.current is None   # playback stopped


def test_power_off_disabled(tmp_path):
    app, player, _ = build_app(
        tmp_path, initial_volume=0, power_off_on_min_volume=False
    )
    app.start()
    send(app, Action.VOLUME_DOWN)   # at 0, but feature disabled
    assert app.powered_off is False


def test_mute_toggle_and_unmute_on_volume(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.MUTE)
    assert app.muted and player.muted
    send(app, Action.VOLUME_UP)  # changing volume unmutes
    assert not app.muted and not player.muted


def test_direct_channel_entry_with_enter(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.DIGIT, 4)
    assert app.lineup.current.number == 2  # not committed yet
    send(app, Action.ENTER)
    assert app.lineup.current.number == 4


def test_direct_channel_entry_times_out(tmp_path):
    app, player, clock = build_app(tmp_path)
    app.start()
    send(app, Action.DIGIT, 3)
    assert app.lineup.current.number == 2
    clock.advance(2.1)  # past the entry timeout
    app.step()
    assert app.lineup.current.number == 3


def test_invalid_channel_entry_shows_message(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    assert app.select_channel_number(99) is False
    assert "NO CHANNEL" in player.overlays.get(4, "")
    assert app.lineup.current.number == 2  # unchanged


def test_last_channel_jump(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.CHANNEL_UP)  # now on 3, last=2
    assert app.lineup.current.number == 3
    send(app, Action.LAST_CHANNEL)
    assert app.lineup.current.number == 2
    send(app, Action.LAST_CHANNEL)  # bounces back to 3
    assert app.lineup.current.number == 3


def test_episode_advances_on_end(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    first = player.current
    player.finish_current(END_EOF)  # simulate the episode ending
    app._drain_playback_events()
    assert player.current is not None
    assert player.current != first  # rolled into the next shuffled episode


def test_standby_blanks_and_ignores_input(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.POWER)
    assert app.standby
    assert player.current is None  # screen blanked
    assert 3 in player.overlays  # standby overlay
    # input is ignored while in standby
    send(app, Action.CHANNEL_UP)
    assert app.lineup.current.number == 2
    # power again wakes it up and resumes playback
    send(app, Action.POWER)
    assert not app.standby
    assert player.current is not None


def test_quit_stops_running(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    app._running = True
    send(app, Action.QUIT)
    assert app._running is False


def test_glitch_transition_then_episode(tmp_path):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "glitch.mp4").write_bytes(b"\x00")
    app, player, clock = build_app(tmp_path, assets_dir=assets, transition="glitch")
    app.start()
    send(app, Action.CHANNEL_UP)
    # A glitch->episode transition was issued (glitch clip + preloaded episode).
    assert player.transitions, "expected a transition on channel change"
    clip, target, _start = player.transitions[-1]
    assert clip == assets / "glitch.mp4"
    assert player.current == target  # the episode is what plays


def test_transition_none_cuts_straight(tmp_path):
    # bridge_seconds=0 -> switch immediately, no transition clip, no preload
    app, player, _ = build_app(tmp_path, transition="none", bridge_seconds=0)
    app.start()
    first = player.current
    send(app, Action.CHANNEL_UP)
    assert not player.transitions
    assert player.preloaded is None
    assert player.current is not None and player.current != first


def test_channel_change_bridges_current_until_next_ready(tmp_path):
    # With bridge_seconds>0 and no transition, the current show keeps playing
    # while the next channel preloads, then cuts over after the window.
    app, player, clock = build_app(tmp_path, bridge_seconds=0.8)
    app.start()
    first = player.current
    send(app, Action.CHANNEL_UP)
    assert player.current == first          # old show still playing...
    assert player.preloaded is not None     # ...next channel preloading
    clock.advance(1.0)
    app.step()                              # bridge window elapsed -> switch
    assert player.preloaded is None
    assert player.current is not None and player.current != first


def test_advance_within_channel_has_no_transition(tmp_path):
    # An episode ending should roll straight into the next one (no glitch burst).
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "glitch.mp4").write_bytes(b"\x00")
    app, player, _ = build_app(tmp_path, assets_dir=assets, transition="glitch")
    app.start()
    before = len(player.transitions)
    player.finish_current(END_EOF)
    app._drain_playback_events()
    assert len(player.transitions) == before  # no new transition
    assert player.current is not None


def test_start_offset_applied(tmp_path):
    app, player, _ = build_app(tmp_path, start_offset=5)
    app.start()
    # The episode should begin 5 seconds in, not at the very beginning.
    assert player.played[-1][1] == 5.0


def test_start_offset_range_applied(tmp_path):
    app, player, _ = build_app(tmp_path, start_offset=[6, 10])
    app.start()
    assert 6.0 <= player.played[-1][1] <= 10.0


def test_empty_channel_shows_no_signal(tmp_path):
    (tmp_path / "dragon").mkdir()
    make_show(tmp_path, "arthur", 2)
    config = config_from_dict(
        {
            "channels": [
                {"number": 2, "name": "Dragon Tales", "path": str(tmp_path / "dragon")},
                {"number": 3, "name": "Arthur", "path": str(tmp_path / "arthur")},
            ]
        }
    )
    app = TVApp(config, MockPlayer(), InputManager([]), clock=FakeClock())
    app.start()  # starts on ch 2 which is empty
    assert "NO SIGNAL" in app.player.overlays.get(4, "")


def test_channel_banner_deferred_until_switch(tmp_path):
    app, player, clock = build_app(tmp_path, bridge_seconds=0.8)
    app.start()
    player.overlays.pop(1, None)          # clear the power-on banner
    send(app, Action.CHANNEL_UP)
    assert 1 not in player.overlays       # banner NOT shown during the bridge
    clock.advance(1.0)
    app.step()                            # cut-over happens here
    assert "CH 03" in player.overlays.get(1, "")  # banner appears at the switch


def test_resume_mode_restarts_where_left(tmp_path):
    # bridge_seconds=0 keeps this test focused on resume (immediate switches)
    app, player, _ = build_app(tmp_path, tune_in="resume", bridge_seconds=0)
    app.start()
    playing = player.current
    player.time_pos = 42.0
    send(app, Action.CHANNEL_UP)  # leave ch 2, remembering position 42
    send(app, Action.CHANNEL_DOWN)  # back to ch 2 -> resume at 42
    assert player.current == playing
    assert player.played[-1] == (playing, 42.0)


# -- admin/developer view ---------------------------------------------------
# The secret trigger itself (a long power-button hold) is detected in the
# evdev keyboard backend, which needs real hardware libraries to import and
# is intentionally untested here (see tests/test_keymap.py for the pure
# key-mapping pieces). These tests drive the app the same way that backend
# would: by delivering the Action.ADMIN_TOGGLE event it emits on release.
#
# Admin mode has three nested screens: the show grid (admin_browsing), a
# show's episode list (admin_episode_browsing) once confirmed, and the
# Insights view (admin_insights_viewing, see further down). All three are
# drawn as ASS overlays on top of a pre-composed poster-grid background
# image (see nostalgiabox.thumbnails) - mpv itself never closes for any of
# this (see app.py's module docstring for why: a real browser UI briefly
# did close it, and that reliably segfaulted the box on real hardware).
# Confirming an episode/continue entry is the only thing that actually
# changes what's playing - selecting a show just opens its episode list.


def test_admin_toggle_opens_show_grid(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    assert not app.admin_mode
    send(app, Action.ADMIN_TOGGLE)
    assert app.admin_mode
    assert app.admin_browsing and not app.admin_episode_browsing
    assert app._browse_number == 2  # cursor starts on whatever's playing
    assert player.closed is False  # mpv never closes for browsing (UKE-29)
    panel = player.overlays.get(5, "")
    assert "Select a channel" in panel
    assert "Dragon Tales" in panel and "Arthur" in panel and "Rugrats" in panel
    assert "4 eps" in panel
    assert "Watch Insights" in panel  # the evergreen Insights row (UKE-29)


def test_admin_toggle_off_by_default_mute_still_mutes(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.MUTE)
    assert player.muted is True
    assert player.paused is False  # untouched: mute is still just mute


def test_channel_up_down_no_op_within_a_single_section(tmp_path):
    # 3 channels (2, 3, 4), no games: the "Shows" row is the only row on
    # screen (besides the always-present Insights row - see
    # TVApp._section_keys), so Channel Up (nothing above) has nothing to do,
    # and Channel Down moves straight to Insights.
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    assert app._browse_number == 2
    send(app, Action.CHANNEL_UP)
    assert app._browse_number == 2  # no row above the Shows row
    assert app.lineup.current.number == 2  # cursor moved (well, didn't), playback didn't


def test_volume_up_down_move_within_a_row(tmp_path):
    # Volume Up/Down move horizontally within whichever row the cursor is
    # on - the Shows row here holds all 3 channels, wrapping at the ends.
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.VOLUME_UP)
    assert app._browse_number == 3
    assert app.volume == 70  # real volume untouched while browsing
    send(app, Action.VOLUME_UP)
    assert app._browse_number == 4
    send(app, Action.VOLUME_UP)
    assert app._browse_number == 2  # wraps back
    send(app, Action.VOLUME_DOWN)
    assert app._browse_number == 4


def test_mute_on_grid_opens_episode_list_without_tuning(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.MUTE)  # confirm "Dragon Tales" (already highlighted)
    assert not app.admin_browsing
    assert app.admin_episode_browsing
    assert app._browse_episode_number == 2
    assert app._browse_episode_index == 0
    assert app.lineup.current.number == 2  # selecting a show doesn't tune yet
    assert player.muted is False
    panel = player.overlays.get(5, "")
    assert "Dragon Tales" in panel and "Select an episode" in panel


def test_channel_up_down_move_episode_cursor(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.MUTE)  # into Dragon Tales' episode list (4 episodes)
    send(app, Action.CHANNEL_DOWN)
    assert app._browse_episode_index == 1
    send(app, Action.CHANNEL_UP)
    assert app._browse_episode_index == 0
    send(app, Action.CHANNEL_UP)
    assert app._browse_episode_index == 3  # wraps to the last episode


def test_mute_confirms_episode_and_plays_it(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.MUTE)  # Dragon Tales episode list
    send(app, Action.CHANNEL_DOWN)  # episode index 1
    channel = next(c for c in app.lineup if c.number == 2)
    expected_path = channel.episodes[1]
    send(app, Action.MUTE)  # confirm episode
    assert not app.admin_episode_browsing
    assert not app.admin_browsing
    assert not app.admin_mode  # back to actually watching - grid fully closed (UKE-29)
    assert app.lineup.current.number == 2
    assert player.current == expected_path
    assert player.played[-1] == (expected_path, 0.0)
    assert player.muted is False
    assert 5 not in player.overlays  # nothing admin-related left on screen either


def test_confirming_a_show_and_episode_never_closes_mpv(tmp_path):
    # Regression guard for the DRM-race crash class (UKE-29): browsing the
    # grid/episode list and confirming an episode must never close mpv at
    # all - the poster grid is just another image mpv plays (see
    # _show_admin_grid_background), not a display handoff to a second
    # process. (This used to require an explicit close+reopen around this
    # exact sequence to avoid driving an already-terminated mpv instance,
    # back when the browser-based admin UI closed mpv on entry - see git
    # history if you need that version.)
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    assert player.closed is False
    send(app, Action.MUTE)  # confirm show
    assert player.closed is False
    send(app, Action.MUTE)  # confirm episode
    assert player.closed is False
    assert player.current is not None


def test_power_backs_out_of_episode_list_to_grid(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.MUTE)  # into episode list
    assert app.admin_episode_browsing
    send(app, Action.POWER)
    assert not app.admin_episode_browsing
    assert app.admin_browsing
    assert not app.standby  # power backed out, it did not toggle standby
    assert "Select a channel" in player.overlays.get(5, "")


# -- Insights view (UKE-29) --------------------------------------------------


def _walk_to_insights(app):
    """Move the cursor from wherever the grid opens down to the Insights
    row - it's always the bottommost row (see _section_keys), so Channel
    Down enough times always gets there regardless of what else is on
    screen.
    """
    for _ in range(6):
        if app._current_section() == "insights":
            return
        send(app, Action.CHANNEL_DOWN)
    raise AssertionError("never reached the insights row")


def test_confirming_insights_opens_it_with_no_player_interaction(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)  # swaps mpv over to the poster-grid image
    grid_image = player.current
    _walk_to_insights(app)
    send(app, Action.MUTE)  # confirm

    assert not app.admin_browsing
    assert app.admin_insights_viewing
    assert app.admin_mode  # still in admin mode overall
    # Purely a read-only screen drawn as an overlay on the same grid image -
    # nothing new was ever asked to play, and mpv was never closed either.
    assert player.current == grid_image
    assert player.closed is False

    panel = player.overlays.get(5, "")
    assert "Insights" in panel
    assert "Nothing watched yet" in panel  # nothing seeded in this test


def test_power_backs_out_of_insights_to_grid_on_the_insights_tile(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    _walk_to_insights(app)
    send(app, Action.MUTE)
    assert app.admin_insights_viewing

    send(app, Action.POWER)
    assert not app.admin_insights_viewing
    assert app.admin_browsing
    assert not app.standby  # backed out, did not toggle standby
    assert app._current_section() == "insights"  # landed back on the same tile
    assert "Select a channel" in player.overlays.get(5, "")


def test_admin_toggle_exits_directly_from_insights(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    _walk_to_insights(app)
    send(app, Action.MUTE)
    send(app, Action.ADMIN_TOGGLE)  # long-press exits from anywhere
    assert not app.admin_mode
    assert not app.admin_insights_viewing


def test_insights_reflects_watched_totals(tmp_path):
    ws = WatchState(tmp_path / "watch_state.json")
    app, player, _ = build_app(tmp_path, watch_state=ws)
    app.start()
    channel = next(c for c in app.lineup if c.number == 2)
    ws.mark_episode_watched(2, channel.config.path, channel.episodes[0], duration=600.0)

    send(app, Action.ADMIN_TOGGLE)
    _walk_to_insights(app)
    send(app, Action.MUTE)

    summary, _suggestions = app._current_insights()
    assert summary.total_episodes_watched == 1
    assert summary.total_watched_minutes == 10
    assert summary.favorite.name == "Dragon Tales"

    panel = player.overlays.get(5, "")
    assert "10" in panel  # total minutes watched
    assert "Dragon Tales" in panel  # favorite banner


def test_insights_with_nothing_watched_is_empty_but_valid(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    _walk_to_insights(app)
    send(app, Action.MUTE)

    summary, suggestions = app._current_insights()
    assert summary.total_watched_minutes == 0
    assert summary.total_episodes_watched == 0
    assert summary.total_games_played == 0
    assert summary.favorite is None
    assert summary.activity == []
    assert suggestions == []


def test_admin_toggle_exits_directly_from_episode_list(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.MUTE)  # into episode list
    send(app, Action.ADMIN_TOGGLE)  # long-press exits from anywhere
    assert not app.admin_mode
    assert not app.admin_browsing and not app.admin_episode_browsing


def test_browsing_without_picking_anything_resumes_on_exit(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    playing = player.current
    player.time_pos = 17.5
    send(app, Action.ADMIN_TOGGLE)  # captures pre-admin path/position
    send(app, Action.CHANNEL_DOWN)  # just look around, pick nothing
    send(app, Action.ADMIN_TOGGLE)  # exit without confirming an episode
    assert not app.admin_mode
    assert player.current == playing
    assert player.played[-1] == (playing, 17.5)


def test_mute_becomes_pause_play_once_watching(tmp_path):
    # Pause/play via Mute is an Adult Mode control (UKE-29, see the module
    # docstring) - it does nothing special in Kid Mode (see
    # test_kid_mode_mute_stays_a_real_mute_even_after_watching_an_episode).
    app, player, _ = build_app(tmp_path)
    app.start()
    app.adult_mode = True
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.MUTE)  # confirm show
    send(app, Action.MUTE)  # confirm episode -> now watching
    send(app, Action.MUTE)  # pause
    assert app.paused is True
    assert player.paused is True
    assert player.muted is False  # never touched the real mute
    send(app, Action.MUTE)
    assert app.paused is False
    assert player.paused is False


def test_kid_mode_mute_stays_a_real_mute_even_after_watching_an_episode(tmp_path):
    # Regression guard: without Adult Mode on, picking a show/episode from
    # the grid must not leave Mute repurposed - it's a real mute, exactly as
    # it always was, since Adult Mode (not just having glanced at the grid)
    # is what unlocks pause/seek/subtitles (UKE-29).
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.MUTE)  # confirm show
    send(app, Action.MUTE)  # confirm episode
    assert not app.adult_mode
    send(app, Action.MUTE)
    assert app.muted is True
    assert player.muted is True
    assert app.paused is False


def test_exiting_admin_mode_under_adult_mode_preserves_pause_with_no_panel_left_behind(tmp_path):
    # UKE-29: unlike the old single admin_mode flag (which always force-
    # unpaused on exit), closing the grid under Adult Mode leaves the pause
    # state exactly as it was - and, the actual bug report this fixes,
    # nothing is left glued to the screen either way (no persistent panel).
    app, player, _ = build_app(tmp_path)
    app.start()
    app.adult_mode = True
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.MUTE)  # confirm show
    send(app, Action.MUTE)  # confirm episode
    send(app, Action.MUTE)  # pause
    assert app.paused
    send(app, Action.ADMIN_TOGGLE)  # reopen the grid...
    assert app.admin_mode
    send(app, Action.ADMIN_TOGGLE)  # ...and close it again
    assert not app.admin_mode
    assert not app.admin_browsing and not app.admin_episode_browsing
    assert app.paused  # untouched - Adult Mode kept it
    assert player.paused is True
    assert 5 not in player.overlays  # nothing admin-related left on screen


def test_changing_channel_while_watching_unpauses_and_shows_channel_bug(tmp_path):
    # bridge_seconds=0: channel bug appears immediately instead of waiting on
    # the (pending) cut-over deadline, keeping this test's assertion simple.
    app, player, _ = build_app(tmp_path, bridge_seconds=0)
    app.start()
    app.adult_mode = True
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.MUTE)  # confirm show
    send(app, Action.MUTE)  # confirm episode -> watching channel 2
    send(app, Action.MUTE)  # pause
    assert app.paused
    # Channel Up/Down seek instead of changing channel while Adult Mode is
    # on (see the seek tests below) - turning it off (as if from the grid's
    # toggle row) hands Channel Up/Down back to normal channel-surfing.
    app.adult_mode = False
    send(app, Action.CHANNEL_UP)
    assert not app.paused  # fresh channel: no longer paused
    assert "CH 03" in player.overlays.get(1, "")  # channel bug confirms the real change


def test_channel_up_down_seek_instead_of_changing_channel_under_adult_mode(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    app.adult_mode = True
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.MUTE)  # confirm show
    send(app, Action.MUTE)  # confirm episode -> watching channel 2
    assert not app.admin_mode  # grid is closed - Adult Mode, not browsing, drives this
    assert player.time_pos == 0.0
    send(app, Action.CHANNEL_UP)
    assert player.time_pos == app.config.admin_seek_seconds
    assert app.lineup.current.number == 2  # channel itself never changed
    send(app, Action.CHANNEL_DOWN)
    send(app, Action.CHANNEL_DOWN)
    assert player.time_pos == 0.0  # clamped, doesn't go negative
    assert app.lineup.current.number == 2


def test_channel_up_down_change_channels_normally_without_adult_mode(tmp_path):
    # Regression guard for the other direction: with Adult Mode off, Channel
    # Up/Down keep their normal kid-facing meaning even after watching an
    # episode picked from the grid.
    app, player, _ = build_app(tmp_path, bridge_seconds=0)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.MUTE)  # confirm show
    send(app, Action.MUTE)  # confirm episode -> watching channel 2
    send(app, Action.CHANNEL_UP)
    assert app.lineup.current.number == 3  # a real channel change, not a seek


def test_seek_shows_an_osd_message_with_the_new_position(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    app.adult_mode = True
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.MUTE)  # confirm show
    send(app, Action.MUTE)  # confirm episode
    send(app, Action.CHANNEL_UP)
    message = player.overlays.get(4, "")  # message overlay slot
    assert "»" in message or "10" in message


def test_admin_insights_viewing_blocks_channel_and_volume_actions_from_touching_the_player(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    _walk_to_insights(app)
    send(app, Action.MUTE)  # confirm -> opens insights
    assert app.admin_insights_viewing
    time_pos_before = player.time_pos
    volume_before = player.volume
    muted_before = player.muted
    send(app, Action.CHANNEL_UP)
    send(app, Action.CHANNEL_DOWN)
    send(app, Action.VOLUME_UP)
    send(app, Action.VOLUME_DOWN)
    send(app, Action.MUTE)
    assert player.time_pos == time_pos_before
    assert player.volume == volume_before
    assert player.muted == muted_before
    assert app.admin_insights_viewing  # none of those actions closed the screen either


def test_admin_toggle_ignored_while_in_standby(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.POWER)  # enter standby
    assert app.standby
    send(app, Action.ADMIN_TOGGLE)
    assert not app.admin_mode  # blocked, same as every other action in standby


def test_entering_standby_resets_adult_mode_browsing_and_pause(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    app.adult_mode = True
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.MUTE)  # confirm show
    send(app, Action.MUTE)  # confirm episode
    send(app, Action.MUTE)  # pause
    assert app.adult_mode and app.paused
    send(app, Action.POWER)  # standby
    assert not app.admin_mode
    assert not app.admin_browsing and not app.admin_episode_browsing
    assert not app.admin_insights_viewing
    assert not app.adult_mode  # standby is the kid-proof reset point (UKE-29)
    assert not app.paused
    assert app._pre_admin_path is None
    assert 5 not in player.overlays


# -- Adult Mode toggle, quick episode-switch, subtitles (UKE-29) ------------
# Real-hardware feedback split the old single admin_mode flag into three
# states - see the module docstring. These tests drive the *actual* toggle
# row in the grid (rather than poking app.adult_mode directly, like the
# pause/seek tests above do) to cover _confirm_adult_toggle and its wiring
# into _section_keys/_move_browse_cursor.


def _walk_to_adult_toggle(app):
    for _ in range(8):
        if app._current_section() == "adult_toggle":
            return
        send(app, Action.CHANNEL_DOWN)
    raise AssertionError("never reached the adult mode toggle row")


def test_confirming_the_adult_toggle_flips_it_and_stays_on_the_grid(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    _walk_to_adult_toggle(app)
    assert not app.adult_mode

    send(app, Action.MUTE)  # confirm - flips the toggle
    assert app.adult_mode
    assert app.admin_browsing  # stayed on the grid, unlike confirming a show
    assert "Adult Mode: ON" in player.overlays.get(4, "")  # transient confirmation

    send(app, Action.MUTE)  # flip it back off
    assert not app.adult_mode
    assert "Adult Mode: OFF" in player.overlays.get(4, "")


def test_adult_mode_survives_closing_and_reopening_the_grid(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    _walk_to_adult_toggle(app)
    send(app, Action.MUTE)  # turn it on
    send(app, Action.ADMIN_TOGGLE)  # close the grid entirely
    assert not app.admin_mode
    assert app.adult_mode  # sticky - survives closing the grid (UKE-29)
    send(app, Action.ADMIN_TOGGLE)  # reopen
    assert app.adult_mode  # still on


def test_back_button_reopens_episode_list_under_adult_mode(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    app.adult_mode = True
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.MUTE)  # confirm show (Dragon Tales)
    send(app, Action.CHANNEL_DOWN)  # episode index 1
    channel = next(c for c in app.lineup if c.number == 2)
    send(app, Action.MUTE)  # confirm episode 1 -> watching, grid closed
    assert not app.admin_mode
    playing_before = player.current
    pos_before = player.time_pos

    send(app, Action.LAST_CHANNEL)  # the quick-reopen shortcut (UKE-29)
    assert app.admin_episode_browsing
    assert not app.admin_browsing
    assert app._browse_episode_number == 2
    assert app._browse_episode_index == 1  # preselects the episode actually playing
    # Remembered exactly what was playing so backing out without picking
    # anything new resumes it - same bookkeeping _toggle_admin_mode does.
    assert app._pre_admin_path == playing_before
    assert app._pre_admin_pos == pos_before
    assert "Dragon Tales" in player.overlays.get(5, "")

    # backing out without picking anything new still resumes correctly
    send(app, Action.POWER)  # back to the grid
    send(app, Action.ADMIN_TOGGLE)  # exit entirely
    assert player.current == playing_before


def test_back_button_keeps_normal_meaning_without_adult_mode(tmp_path):
    # Regression guard: Kid Mode (and Admin Mode without Adult Mode on)
    # keeps Last-Channel's ordinary "jump to previous channel" behavior.
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.CHANNEL_UP)  # now on ch 3, last=2
    assert app.lineup.current.number == 3
    send(app, Action.LAST_CHANNEL)
    assert app.lineup.current.number == 2
    assert not app.admin_episode_browsing


def test_back_button_quick_reopen_is_a_noop_on_an_empty_channel(tmp_path):
    (tmp_path / "empty").mkdir()
    config = config_from_dict(
        {"channels": [{"number": 2, "name": "Empty", "path": str(tmp_path / "empty")}]}
    )
    app = TVApp(config, MockPlayer(), InputManager([]), clock=FakeClock())
    app.start()
    app.adult_mode = True
    send(app, Action.LAST_CHANNEL)
    assert not app.admin_episode_browsing


def test_info_toggles_subtitles_under_adult_mode(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    app.adult_mode = True
    assert app.subtitles_visible is False  # config default
    send(app, Action.INFO)
    assert app.subtitles_visible is True
    assert player.subtitles_visible is True
    assert "Subtitles: ON" in player.overlays.get(4, "")
    send(app, Action.INFO)
    assert app.subtitles_visible is False
    assert player.subtitles_visible is False


def test_info_shows_channel_banner_in_kid_mode(tmp_path):
    # Regression guard: Info keeps its original behaviour unless Adult Mode
    # is actively on and nothing's being browsed.
    app, player, _ = build_app(tmp_path)
    app.start()
    player.overlays.pop(1, None)
    send(app, Action.INFO)
    assert 1 in player.overlays  # channel bug shown
    assert player.subtitles_visible is False  # unchanged from start()'s config default


def test_info_shows_channel_banner_while_browsing_even_under_adult_mode(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    app.adult_mode = True
    send(app, Action.ADMIN_TOGGLE)  # browsing - Info shouldn't toggle subtitles here
    player.overlays.pop(1, None)
    send(app, Action.INFO)
    assert 1 in player.overlays
    assert player.subtitles_visible is False


def test_subtitles_default_from_config(tmp_path):
    app, player, _ = build_app(tmp_path, subtitles_default=True)
    app.start()
    assert app.subtitles_visible is True
    assert player.subtitles_visible is True


def test_closing_admin_mode_uses_the_full_canvas_without_the_4x3_pillarbox_filter(tmp_path):
    # UKE-29 real-hardware feedback: the admin UI used to be confined to the
    # 960px-wide 4:3 "tube" every actual show plays in. The background image
    # bypasses that filter (use_frame_filter=False) so it fills the whole
    # widescreen canvas edge to edge instead.
    from PIL import Image

    from nostalgiabox import thumbnails

    assets_dir = tmp_path / "assets"
    thumbs_dir = assets_dir / thumbnails.THUMBS_SUBDIR
    thumbs_dir.mkdir(parents=True)
    Image.new("RGB", (thumbnails.GRID_W, thumbnails.GRID_H), (20, 20, 20)).save(
        thumbs_dir / thumbnails.GRID_FILENAME, quality=88
    )
    app, player, _ = build_app(tmp_path, assets_dir=assets_dir)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    assert player.looping is not None
    assert player.loop_frame_filter[player.looping] is False


def test_admin_grid_scrolls_when_the_cursor_reaches_a_row_below_the_fold(tmp_path):
    # UKE-29: enough shows that the Shows section alone wraps onto multiple
    # rows and overflows one screen (see thumbnails.admin_section_layout /
    # scrollable_content_height) - walking the cursor down into it should
    # scroll the grid and reload the background at the new position.
    from PIL import Image

    from nostalgiabox import thumbnails

    for i in range(10):
        make_show(tmp_path, f"show{i}", 1)
    channels = [
        {"number": 2 + i, "name": f"Show {i}", "path": str(tmp_path / f"show{i}")} for i in range(10)
    ]
    assets_dir = tmp_path / "assets"
    thumbs_dir = assets_dir / thumbnails.THUMBS_SUBDIR
    thumbs_dir.mkdir(parents=True)

    app, player, _ = build_app(tmp_path, assets_dir=assets_dir, channels=channels)
    height = max(thumbnails.GRID_H, thumbnails.scrollable_content_height(list(app.lineup)))
    # A vertical gradient (not a flat colour) - different crop offsets must
    # produce visibly different pixels, otherwise "did it re-crop" can't
    # actually be observed from the output bytes. Built as a 1px column and
    # stretched, which is much faster than pasting per-row.
    column = Image.new("L", (1, height))
    column.putdata([y % 256 for y in range(height)])
    gradient = column.resize((thumbnails.GRID_W, height)).convert("RGB")
    gradient.save(thumbs_dir / thumbnails.GRID_FILENAME, quality=88)
    app.start()

    send(app, Action.ADMIN_TOGGLE)
    assert app._scroll_y == 0
    # crop_viewport always (re)writes the same fixed filename, so identity
    # isn't a useful signal here - snapshot its bytes instead to confirm a
    # fresh crop was actually written at the new scroll position.
    viewport_path = thumbs_dir / thumbnails.VIEWPORT_FILENAME
    unscrolled_bytes = viewport_path.read_bytes()

    # Volume Up/Down walks through the (now multi-row) Shows section - by
    # the last tile the grid must have scrolled to keep it visible.
    for _ in range(9):
        send(app, Action.VOLUME_UP)
    assert app._browse_number == 11  # last of the 10 shows (numbers 2..11)
    scrolled_y = app._scroll_y
    assert scrolled_y > 0
    assert viewport_path.read_bytes() != unscrolled_bytes  # re-cropped at the new offset

    # And scrolling back up to the first tile scrolls back up close to the
    # top - not necessarily to exactly 0, since the first row sits just
    # below the "Shows" section label, not right at the very top edge.
    for _ in range(9):
        send(app, Action.VOLUME_DOWN)
    assert app._browse_number == 2
    assert app._scroll_y < scrolled_y


# -- games: admin-mode arcade via RetroArch (UKE-28) -------------------------
# Games are a second kind of admin-grid tile, alongside real channels (see
# TVApp._admin_tiles): a "system" (SNES) behaves like a channel, and its ROMs
# behave like episodes, reusing the exact same grid / numbered-list nav. The
# one thing that's genuinely different is confirming a selection: a video
# episode plays and exits back to normal viewing, but a game hands the
# display to RetroArch (via the injectable game_launcher). This is the one
# remaining place mpv actually closes (see app.py's module docstring) -
# de-risked on real hardware by scripts/spike_mpv_retroarch_handoff.py.


def _games_override(tmp_path, *, roms=2, ext=".sfc", core="/cores/snes9x.so"):
    make_show(tmp_path, "snes", roms, ext=ext)
    return {
        "games": {
            "systems": [
                {"name": "SNES", "path": str(tmp_path / "snes"), "core": core, "extensions": [ext]}
            ]
        }
    }


def test_game_system_appears_in_admin_grid(tmp_path):
    app, player, _ = build_app(tmp_path, **_games_override(tmp_path, roms=2))
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    panel = player.overlays.get(5, "")
    assert "SNES" in panel
    assert "2 games" in panel
    assert app.games[0].number == 5  # continues on from the highest real channel (4)


def test_can_navigate_onto_and_into_a_game_system(tmp_path):
    app, player, _ = build_app(tmp_path, **_games_override(tmp_path))
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    # Shows row = [2, 3, 4], Games row = [5 (SNES)] - Channel Down moves
    # between rows, landing on the first tile of the row below.
    send(app, Action.CHANNEL_DOWN)
    assert app._browse_number == 5
    send(app, Action.MUTE)  # confirm SNES -> its game list
    assert app.admin_episode_browsing
    assert app._browse_episode_number == 5
    panel = player.overlays.get(5, "")
    assert "SNES" in panel and "Select a game" in panel


def test_confirming_a_game_calls_the_launcher_and_stays_on_the_list(tmp_path):
    calls = []

    def fake_launcher(core, rom):
        calls.append((core, rom))
        return 0

    app, player, _ = build_app(
        tmp_path, game_launcher=fake_launcher, **_games_override(tmp_path, roms=2)
    )
    app.start()
    playing = player.current
    player.time_pos = 12.0
    send(app, Action.ADMIN_TOGGLE)  # captures pre_admin_path/pos
    send(app, Action.CHANNEL_DOWN)
    send(app, Action.VOLUME_UP)  # cursor -> SNES (5)
    send(app, Action.MUTE)  # into SNES's game list
    rom = app.games[0].episodes[0]

    send(app, Action.MUTE)  # confirm the first game

    assert calls == [("/cores/snes9x.so", rom)]
    # Stayed on exactly the same game list - unlike picking a show episode,
    # nothing "started playing" in the mpv sense.
    assert app.admin_episode_browsing
    assert app._browse_episode_number == 5
    assert app.admin_mode
    # The video playing before admin mode opened is untouched by the game -
    # launching a game must not clear the "nothing new is playing" resume state.
    assert app._pre_admin_path == playing
    assert app._pre_admin_pos == 12.0


def test_confirming_a_second_game_works_after_returning_to_the_list(tmp_path):
    calls = []
    app, player, _ = build_app(
        tmp_path,
        game_launcher=lambda core, rom: calls.append(rom) or 0,
        **_games_override(tmp_path, roms=2),
    )
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.CHANNEL_DOWN)
    send(app, Action.VOLUME_UP)
    send(app, Action.MUTE)  # into SNES's game list
    send(app, Action.MUTE)  # play game 1
    send(app, Action.CHANNEL_DOWN)  # move to game 2
    send(app, Action.MUTE)  # play game 2
    assert calls == list(app.games[0].episodes)  # both, in order


def test_game_launch_stops_and_recreates_player_via_factory(tmp_path):
    created = []

    def factory():
        p = MockPlayer()
        created.append(p)
        return p

    app, player1, _ = build_app(
        tmp_path,
        game_launcher=lambda core, rom: 0,
        player_factory=factory,
        **_games_override(tmp_path),
    )
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.CHANNEL_DOWN)
    send(app, Action.VOLUME_UP)
    send(app, Action.MUTE)  # into SNES's game list
    send(app, Action.MUTE)  # confirm and launch

    assert player1.closed is True
    assert len(created) == 1
    assert app.player is created[0]
    assert app.player is not player1
    # volume/mute state carried over onto the freshly (re)created player
    assert app.player.volume == app.volume
    assert app.player.muted == app.muted


def test_game_launch_without_a_player_factory_reuses_the_same_player(tmp_path):
    # No factory (the common case in tests / a dry run) - the same player
    # instance is kept, just close()d then reused for the grid image again.
    app, player, _ = build_app(
        tmp_path, game_launcher=lambda core, rom: 0, **_games_override(tmp_path)
    )
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.CHANNEL_DOWN)
    send(app, Action.VOLUME_UP)
    send(app, Action.MUTE)
    send(app, Action.MUTE)
    assert app.player is player
    assert player.closed is True


def test_launch_game_with_no_core_configured_is_a_no_op(tmp_path):
    # Not reachable through normal config (kind="game" requires 'core' - see
    # config.py's ChannelConfig validation) - this exercises _launch_game's
    # own defensive guard directly.
    from nostalgiabox.channel import Channel
    from nostalgiabox.config import ChannelConfig

    calls = []
    app, player, _ = build_app(tmp_path, game_launcher=lambda c, r: calls.append((c, r)))
    bad_cfg = ChannelConfig(number=99, name="Broken", path=tmp_path)  # kind defaults to "show", core=None
    channel = Channel(bad_cfg, [])
    app._launch_game(channel, tmp_path / "rom.sfc")
    assert calls == []
    assert player.closed is False  # bailed out before touching the player


def test_show_episodes_still_exit_admin_browsing_normally(tmp_path):
    # Regression guard: mixing games into _admin_tiles/_confirm_episode_selection
    # must not change how picking an ordinary show episode behaves.
    app, player, _ = build_app(tmp_path, **_games_override(tmp_path))
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.MUTE)  # confirm Dragon Tales (still the first real channel)
    send(app, Action.MUTE)  # confirm an episode
    assert not app.admin_episode_browsing
    assert not app.admin_browsing
    assert not app.admin_mode
    assert player.current is not None


def test_drm_handoff_delay_pauses_around_a_game_launch(tmp_path, monkeypatch):
    # Real-hardware regression (UKE-29): the DRM master race that segfaulted
    # the box when a second process (the now-reverted browser admin UI) had
    # to fight mpv for the display. The one remaining display handoff -
    # launching a game via RetroArch - gets the same defensive pause as
    # cheap insurance, even though it was separately validated safe without
    # it (scripts/spike_mpv_retroarch_handoff.py).
    sleeps = []
    monkeypatch.setattr("nostalgiabox.app.time.sleep", lambda s: sleeps.append(s))
    app, player, _ = build_app(
        tmp_path,
        game_launcher=lambda core, rom: 0,
        player_factory=MockPlayer,
        drm_handoff_delay=0.5,
        **_games_override(tmp_path),
    )
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.CHANNEL_DOWN)
    send(app, Action.VOLUME_UP)
    send(app, Action.MUTE)  # into SNES's game list
    send(app, Action.MUTE)  # confirm and launch
    # once before handing off to RetroArch, once before rebuilding mpv
    assert sleeps == [0.5, 0.5]


def test_drm_handoff_delay_defaults_to_no_pause_around_game_launch(tmp_path, monkeypatch):
    # The default (used by every other test, and by dry-run) - there's no
    # real DRM device to race over, and a real sleep would only slow things
    # down for no benefit.
    sleeps = []
    monkeypatch.setattr("nostalgiabox.app.time.sleep", lambda s: sleeps.append(s))
    app, player, _ = build_app(
        tmp_path, game_launcher=lambda core, rom: 0, player_factory=MockPlayer, **_games_override(tmp_path)
    )
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.CHANNEL_DOWN)
    send(app, Action.VOLUME_UP)
    send(app, Action.MUTE)
    send(app, Action.MUTE)
    assert sleeps == []


# -- watch state: continue-watching / watched / play tracking (UKE-29) ------
# None by default (build_app doesn't pass one unless a test opts in), so the
# many tests above never touch disk. These tests explicitly construct a
# WatchState under tmp_path and pass it in to exercise the real hooks:
# _record_watch_progress (channel change / standby), _mark_current_watched
# (natural end-of-file), and the game-launch played/play_count tracking in
# _launch_game.


def test_no_watch_state_configured_is_a_no_op(tmp_path):
    # Default build_app() passes watch_state=None - nothing should crash,
    # and there's nothing to assert on since tracking is simply off.
    app, player, _ = build_app(tmp_path)
    app.start()
    player.time_pos = 42.0
    send(app, Action.CHANNEL_UP)
    assert app.watch_state is None


def test_channel_change_records_watch_progress(tmp_path, monkeypatch):
    import nostalgiabox.probe as probe_mod

    monkeypatch.setattr(probe_mod, "probe_duration", lambda p: 1320.0)
    ws = WatchState(tmp_path / "watch_state.json")
    app, player, _ = build_app(tmp_path, watch_state=ws)
    app.start()
    playing = player.current
    channel = app.lineup.current
    player.time_pos = 300.0
    send(app, Action.CHANNEL_UP)  # leaves channel 2, should record progress on `playing`

    state = ws.episode_state(channel.number, channel.config.path, playing)
    assert state.position == 300.0
    assert state.duration == 1320.0
    assert state.watched is False
    assert state.in_progress is True


def test_watch_progress_recorded_regardless_of_tune_in_mode(tmp_path, monkeypatch):
    # _remember_position's own Channel.remember() call is gated on
    # tune_in == "resume", but watch-state tracking must not be - the
    # default tune_in ("random") must still track progress.
    import nostalgiabox.probe as probe_mod

    monkeypatch.setattr(probe_mod, "probe_duration", lambda p: 1320.0)
    ws = WatchState(tmp_path / "watch_state.json")
    app, player, _ = build_app(tmp_path, watch_state=ws, tune_in="random")
    app.start()
    playing = player.current
    channel = app.lineup.current
    player.time_pos = 100.0
    send(app, Action.CHANNEL_UP)
    assert ws.episode_state(channel.number, channel.config.path, playing).position == 100.0


def test_standby_also_records_watch_progress(tmp_path, monkeypatch):
    import nostalgiabox.probe as probe_mod

    monkeypatch.setattr(probe_mod, "probe_duration", lambda p: 1320.0)
    ws = WatchState(tmp_path / "watch_state.json")
    app, player, _ = build_app(tmp_path, watch_state=ws)
    app.start()
    playing = player.current
    channel = app.lineup.current
    player.time_pos = 900.0
    send(app, Action.POWER)  # enter standby
    assert ws.episode_state(channel.number, channel.config.path, playing).position == 900.0


def test_episode_reaching_natural_end_is_marked_watched(tmp_path):
    ws = WatchState(tmp_path / "watch_state.json")
    app, player, _ = build_app(tmp_path, watch_state=ws)
    app.start()
    playing = player.current
    channel = app.lineup.current
    player.finish_current(END_EOF)
    app._drain_playback_events()
    state = ws.episode_state(channel.number, channel.config.path, playing)
    assert state.watched is True


def test_episode_error_is_not_marked_watched(tmp_path):
    from nostalgiabox.player import END_ERROR

    ws = WatchState(tmp_path / "watch_state.json")
    app, player, _ = build_app(tmp_path, watch_state=ws)
    app.start()
    playing = player.current
    channel = app.lineup.current
    player.finish_current(END_ERROR)
    app._drain_playback_events()
    state = ws.episode_state(channel.number, channel.config.path, playing)
    assert state.watched is False


def test_confirming_a_game_records_played_and_play_count(tmp_path):
    ws = WatchState(tmp_path / "watch_state.json")
    app, player, _ = build_app(
        tmp_path, watch_state=ws, game_launcher=lambda c, r: 0, **_games_override(tmp_path)
    )
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.CHANNEL_DOWN)
    send(app, Action.VOLUME_UP)  # cursor -> SNES
    send(app, Action.MUTE)  # into SNES's game list
    send(app, Action.MUTE)  # confirm and launch the first game

    system = app.games[0]
    rom = system.episodes[0]
    state = ws.game_state(system.number, system.config.path, rom)
    assert state.played is True
    assert state.play_count == 1

    send(app, Action.MUTE)  # play it again
    assert ws.game_state(system.number, system.config.path, rom).play_count == 2


def test_watch_state_survives_across_app_restarts(tmp_path, monkeypatch):
    # The whole point: unlike the pre-existing per-channel resume position,
    # this needs to actually be on disk, not just in memory.
    import nostalgiabox.probe as probe_mod

    monkeypatch.setattr(probe_mod, "probe_duration", lambda p: 1320.0)
    state_path = tmp_path / "watch_state.json"

    ws1 = WatchState(state_path)
    app1, player1, _ = build_app(tmp_path, watch_state=ws1)
    app1.start()
    playing = player1.current
    channel_number = app1.lineup.current.number
    channel_path = app1.lineup.current.config.path
    player1.time_pos = 500.0
    send(app1, Action.CHANNEL_UP)

    ws2 = WatchState(state_path)  # simulates a fresh process starting up
    state = ws2.episode_state(channel_number, channel_path, playing)
    assert state.position == 500.0


# -- Continue Watching row (UKE-29) ------------------------------------------
# A text-only row above the channel/game grid, showing in-progress episodes
# so they can be resumed directly rather than dug back out of a channel's
# episode list. Cursor lives at self._browse_continue_index while focused
# there (None the rest of the time, which is most of the time - see
# TVApp._move_browse_cursor). These tests use the default 3-channel
# build_app() layout.


def _seed_in_progress(app, ws, channel_number, *, index=0, position=200.0, duration=1200.0):
    channel = next(c for c in app.lineup if c.number == channel_number)
    episode = channel.episodes[index]
    ws.record_episode_position(channel_number, channel.config.path, episode, position=position, duration=duration)
    return episode


def test_no_continue_row_when_nothing_in_progress(tmp_path):
    ws = WatchState(tmp_path / "watch_state.json")
    app, player, _ = build_app(tmp_path, watch_state=ws)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    assert app._continue_entries == []
    send(app, Action.CHANNEL_UP)  # no continue row, and nothing above the Shows row either
    assert app._browse_continue_index is None
    assert app._browse_number == 2  # unchanged - no wraparound between rows


def test_channel_up_from_grid_top_row_enters_continue_row(tmp_path):
    ws = WatchState(tmp_path / "watch_state.json")
    app, player, _ = build_app(tmp_path, watch_state=ws)
    app.start()
    _seed_in_progress(app, ws, 2)
    send(app, Action.ADMIN_TOGGLE)
    assert app._browse_continue_index is None  # opening still lands on the grid
    assert len(app._continue_entries) == 1

    send(app, Action.CHANNEL_UP)  # row 0 -> continue row
    assert app._browse_continue_index == 0

    send(app, Action.CHANNEL_DOWN)  # continue row -> back onto the grid
    assert app._browse_continue_index is None
    assert app._browse_number == 2


def test_volume_moves_within_continue_row_not_the_grid(tmp_path):
    ws = WatchState(tmp_path / "watch_state.json")
    app, player, _ = build_app(tmp_path, watch_state=ws)
    app.start()
    _seed_in_progress(app, ws, 2, position=200.0)
    _seed_in_progress(app, ws, 3, position=300.0)  # recorded later -> sorts first
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.CHANNEL_UP)
    assert app._browse_continue_index == 0
    before = app._browse_number

    send(app, Action.VOLUME_UP)
    assert app._browse_continue_index == 1
    assert app._browse_number == before  # grid cursor untouched while on the row

    send(app, Action.VOLUME_UP)
    assert app._browse_continue_index == 0  # wraps within the row


def test_confirming_continue_entry_resumes_at_saved_position(tmp_path):
    ws = WatchState(tmp_path / "watch_state.json")
    app, player, _ = build_app(tmp_path, watch_state=ws)
    app.start()
    episode = _seed_in_progress(app, ws, 3, position=250.0, duration=1200.0)
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.CHANNEL_UP)  # onto the continue row
    assert app._browse_continue_index == 0

    send(app, Action.MUTE)  # confirm - resumes immediately, no episode list

    assert not app.admin_browsing
    assert app._browse_continue_index is None
    assert app.lineup.current.number == 3
    assert player.current == episode
    assert player.played[-1] == (episode, 250.0)


def test_confirming_a_continue_entry_never_closes_mpv(tmp_path):
    # Same guard as test_confirming_a_show_and_episode_never_closes_mpv,
    # for the other path that plays something directly from admin mode
    # without going through the episode list (UKE-29).
    ws = WatchState(tmp_path / "watch_state.json")
    app, player, _ = build_app(tmp_path, watch_state=ws)
    app.start()
    episode = _seed_in_progress(app, ws, 3, position=250.0, duration=1200.0)
    send(app, Action.ADMIN_TOGGLE)
    assert player.closed is False
    send(app, Action.CHANNEL_UP)  # onto the continue row
    send(app, Action.MUTE)  # confirm
    assert player.closed is False
    assert player.current == episode


def test_continue_entries_recomputed_on_reopening_admin_mode(tmp_path):
    ws = WatchState(tmp_path / "watch_state.json")
    app, player, _ = build_app(tmp_path, watch_state=ws)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    assert app._continue_entries == []
    send(app, Action.ADMIN_TOGGLE)  # close

    _seed_in_progress(app, ws, 2)
    send(app, Action.ADMIN_TOGGLE)  # reopen - should pick up the new state
    assert len(app._continue_entries) == 1


# -- Netflix-style swimlane redesign (UKE-29) --------------------------------
# The admin browse screen is a stack of independent single-row "sections" -
# Continue Watching, Shows, Games (each only present if non-empty), then the
# always-present Insights row. Channel Up/Down move between rows and stop at
# the top/bottom; Volume Up/Down move within a row, wrapping at its ends;
# moving onto a new row always lands on its first tile.


def test_channel_down_walks_through_every_present_section(tmp_path):
    ws = WatchState(tmp_path / "watch_state.json")
    app, player, _ = build_app(tmp_path, watch_state=ws, **_games_override(tmp_path))
    app.start()
    _seed_in_progress(app, ws, 3)
    send(app, Action.ADMIN_TOGGLE)
    assert app._section_keys() == ["continue", "shows", "games", "insights", "adult_toggle"]
    assert app._current_section() == "shows"  # opening always lands on the grid, not the row

    send(app, Action.CHANNEL_UP)
    assert app._current_section() == "continue"
    send(app, Action.CHANNEL_UP)
    assert app._current_section() == "continue"  # topmost row - nowhere further up

    send(app, Action.CHANNEL_DOWN)
    assert app._current_section() == "shows"
    assert app._browse_number == 2  # landed on the row's first tile
    send(app, Action.CHANNEL_DOWN)
    assert app._current_section() == "games"
    assert app._browse_number == 5  # SNES, the games row's first (only) tile
    send(app, Action.CHANNEL_DOWN)
    assert app._current_section() == "insights"
    send(app, Action.CHANNEL_DOWN)
    assert app._current_section() == "adult_toggle"
    send(app, Action.CHANNEL_DOWN)
    assert app._current_section() == "adult_toggle"  # bottommost row - nowhere further down


def test_volume_wraps_within_the_games_row(tmp_path):
    app, player, _ = build_app(tmp_path, **_games_override(tmp_path, roms=2))
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.CHANNEL_DOWN)  # onto the (single-system) Games row
    assert app._browse_number == 5
    send(app, Action.VOLUME_UP)
    assert app._browse_number == 5  # only one game system - wraps to itself
