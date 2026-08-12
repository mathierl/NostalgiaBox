import pytest

from nostalgiabox.actions import Action, InputEvent
from nostalgiabox.app import TVApp
from nostalgiabox.config import config_from_dict
from nostalgiabox.input.manager import InputManager
from nostalgiabox.player import END_EOF, MockPlayer
from tests.helpers import FakeClock, make_show


def build_app(tmp_path, *, assets_dir=None, **overrides):
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


def test_admin_toggle_opens_browse_grid_listing_all_channels(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    assert not app.admin_mode
    send(app, Action.ADMIN_TOGGLE)
    assert app.admin_mode
    assert app.admin_browsing  # opens straight into the "pick a channel" grid
    assert app._browse_number == 2  # cursor starts on whatever's playing
    panel = player.overlays.get(5, "")
    assert "SELECT A CHANNEL" in panel
    assert "Dragon Tales" in panel and "Arthur" in panel and "Rugrats" in panel
    assert "(4 eps)" in panel


def test_admin_toggle_off_by_default_mute_still_mutes(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.MUTE)
    assert player.muted is True
    assert player.paused is False  # untouched: mute is still just mute


def test_channel_up_down_moves_browse_cursor_without_tuning(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.CHANNEL_UP)
    assert app._browse_number == 3  # cursor moved...
    assert app.lineup.current.number == 2  # ...but playback didn't
    assert "> CH 03" in player.overlays.get(5, "")
    send(app, Action.CHANNEL_UP)
    assert app._browse_number == 4
    send(app, Action.CHANNEL_UP)
    assert app._browse_number == 2  # wraps
    send(app, Action.CHANNEL_DOWN)
    assert app._browse_number == 4  # wraps back


def test_mute_confirms_browse_selection_and_tunes(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.CHANNEL_UP)  # cursor -> channel 3
    send(app, Action.MUTE)  # confirm
    assert not app.admin_browsing  # back to the corner panel
    assert app.lineup.current.number == 3  # actually tuned there
    assert player.muted is False  # confirm never touches real mute
    panel = player.overlays.get(5, "")
    assert "SELECT A CHANNEL" not in panel  # corner panel, not the grid


def test_mute_confirms_same_channel_still_closes_grid(tmp_path):
    # Re-selecting the already-current channel takes select_channel_number's
    # early-return path (no tune_current call) - the grid must still close.
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.MUTE)  # confirm channel 2 (already current)
    assert not app.admin_browsing
    assert app.lineup.current.number == 2
    assert "SELECT A CHANNEL" not in player.overlays.get(5, "")


def test_mute_becomes_pause_play_after_confirming_selection(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.MUTE)  # confirm -> leaves browsing
    send(app, Action.MUTE)  # now pause
    assert app.paused is True
    assert player.paused is True
    assert player.muted is False  # never touched the real mute
    send(app, Action.MUTE)
    assert app.paused is False
    assert player.paused is False


def test_exiting_admin_mode_unpauses_and_clears_panel(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.MUTE)  # confirm selection
    send(app, Action.MUTE)  # pause
    assert app.paused
    send(app, Action.ADMIN_TOGGLE)  # exit admin mode
    assert not app.admin_mode
    assert not app.admin_browsing
    assert not app.paused
    assert player.paused is False
    assert 5 not in player.overlays  # admin panel cleared


def test_changing_channel_after_browsing_unpauses_and_refreshes_panel(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.MUTE)  # confirm channel 2
    send(app, Action.MUTE)  # pause on channel 2
    assert app.paused
    send(app, Action.CHANNEL_UP)  # normal channel change now that we're not browsing
    assert not app.paused  # fresh channel: no longer paused
    assert "CH 03" in player.overlays.get(5, "") or "> CH 03" in player.overlays.get(5, "")


def test_admin_toggle_ignored_while_in_standby(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.POWER)  # enter standby
    assert app.standby
    send(app, Action.ADMIN_TOGGLE)
    assert not app.admin_mode  # blocked, same as every other action in standby


def test_entering_standby_resets_admin_mode_browsing_and_pause(tmp_path):
    app, player, _ = build_app(tmp_path)
    app.start()
    send(app, Action.ADMIN_TOGGLE)
    send(app, Action.MUTE)  # confirm selection
    send(app, Action.MUTE)  # pause
    assert app.admin_mode and app.paused
    send(app, Action.POWER)  # standby
    assert not app.admin_mode
    assert not app.admin_browsing
    assert not app.paused
    assert 5 not in player.overlays
