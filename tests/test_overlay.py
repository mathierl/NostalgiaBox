import re

from nostalgiabox.config import config_from_dict
from nostalgiabox.overlay import OverlayManager
from nostalgiabox.player import MockPlayer
from tests.helpers import FakeClock, make_show

# The 4:3 frame within the 1280x720 canvas spans x in [160, 1120].
_FRAME_X0, _FRAME_X1 = 160, 1120


def _all_x_positions(ass: str):
    return [int(m) for m in re.findall(r"\\pos\((\d+),", ass)]


def _config(tmp_path):
    make_show(tmp_path, "a", 1)
    return config_from_dict(
        {
            "channel_bug_seconds": 4,
            "osd_duration": 2,
            "channels": [{"number": 3, "name": "Arthur", "path": str(tmp_path / "a")}],
        }
    )


def test_channel_bug_drawn_and_expires(tmp_path):
    clock = FakeClock()
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=clock)

    om.show_channel_bug(3, "Arthur")
    assert 1 in player.overlays  # channel overlay id
    ass = player.overlays[1]
    assert "CH 03" in ass and "Arthur" in ass

    clock.advance(3.9)
    om.tick()
    assert 1 in player.overlays  # not yet expired

    clock.advance(0.2)
    om.tick()
    assert 1 not in player.overlays  # expired after 4s


def test_volume_overlay_has_label_and_bars(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_volume(45, muted=False)
    ass = player.overlays[2]
    assert "Volume" in ass
    # 20 segments: some drawn as bars (rectangles start "m 0 0 l"), rest as dots.
    assert ass.count("\\p1") == 20


def test_volume_bars_scale_with_level(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_volume(100, muted=False)
    full = player.overlays[2].count("m 0 0 l")  # rectangle (filled bar) count
    om.show_volume(0, muted=False)
    empty = player.overlays[2].count("m 0 0 l")
    assert full == 20 and empty == 0


def test_muted_volume_overlay(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_volume(45, muted=True)
    assert "Mute" in player.overlays[2]


def test_standby_overlay_does_not_expire(tmp_path):
    clock = FakeClock()
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=clock)
    om.show_standby()
    clock.advance(1000)
    om.tick()
    assert 3 in player.overlays  # standby id persists
    om.clear_standby()
    assert 3 not in player.overlays


def test_channel_name_with_braces_is_escaped(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_channel_bug(5, "Weird{name}")
    # Braces in the name must be neutralised (they delimit ASS override blocks).
    ass = player.overlays[1]
    assert "Weird(name)" in ass
    assert "Weird{name}" not in ass


def test_message_overlay(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_message("CH 12  -  NO CHANNEL")
    assert "NO CHANNEL" in player.overlays[4]


def test_channel_bug_sits_inside_4x3_frame(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_channel_bug(3, "Arthur")
    xs = _all_x_positions(player.overlays[1])
    assert xs and all(_FRAME_X0 <= x <= _FRAME_X1 for x in xs)


def test_volume_bar_sits_inside_4x3_frame(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_volume(100, muted=False)  # widest case: all 20 bars drawn
    xs = _all_x_positions(player.overlays[2])
    assert xs and all(_FRAME_X0 <= x <= _FRAME_X1 for x in xs)


def test_overlay_uses_configured_font_and_color(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_channel_bug(3, "Arthur")
    ass = player.overlays[1]
    assert "\\fnVT323" in ass          # bundled retro font
    assert "&H005AFF4D" in ass         # #4DFF5A -> ASS BBGGRR


def test_admin_panel_lists_channels_and_episode_counts(tmp_path):
    from nostalgiabox.channel import build_lineup

    make_show(tmp_path, "a", 1)
    make_show(tmp_path, "b", 3)
    config = config_from_dict(
        {
            "shuffle_seed": 1,
            "channels": [
                {"number": 3, "name": "Arthur", "path": str(tmp_path / "a")},
                {"number": 4, "name": "Bugs", "path": str(tmp_path / "b")},
            ],
        }
    )
    lineup = build_lineup(config)
    player = MockPlayer()
    om = OverlayManager(player, config, clock=FakeClock())
    om.show_admin_panel(lineup, paused=False)
    ass = player.overlays[5]
    assert "Arthur" in ass and "Bugs" in ass
    assert "(1 ep)" in ass  # channel "a" has 1 episode (singular)
    assert "(3 eps)" in ass  # channel "b" has 3 episodes


def test_admin_panel_shows_paused_state(tmp_path):
    from nostalgiabox.channel import build_lineup

    config = _config(tmp_path)
    lineup = build_lineup(config)
    player = MockPlayer()
    om = OverlayManager(player, config, clock=FakeClock())
    om.show_admin_panel(lineup, paused=True)
    assert "PAUSED" in player.overlays[5]


def test_admin_panel_persists_and_clears(tmp_path):
    from nostalgiabox.channel import build_lineup

    config = _config(tmp_path)
    lineup = build_lineup(config)
    player = MockPlayer()
    clock = FakeClock()
    om = OverlayManager(player, config, clock=clock)
    om.show_admin_panel(lineup, paused=False)
    clock.advance(10_000)
    om.tick()
    assert 5 in player.overlays  # persistent, no expiry
    om.clear_admin_panel()
    assert 5 not in player.overlays


def test_clear_all_also_clears_admin_panel(tmp_path):
    from nostalgiabox.channel import build_lineup

    config = _config(tmp_path)
    lineup = build_lineup(config)
    player = MockPlayer()
    om = OverlayManager(player, config, clock=FakeClock())
    om.show_admin_panel(lineup, paused=False)
    om.clear_all()
    assert 5 not in player.overlays


def test_admin_browser_highlights_selected_channel(tmp_path):
    from nostalgiabox.channel import build_lineup

    make_show(tmp_path, "b", 3)
    config = config_from_dict(
        {
            "shuffle_seed": 1,
            "channels": [
                {"number": 3, "name": "Arthur", "path": str(tmp_path / "a")},
                {"number": 4, "name": "Bugs", "path": str(tmp_path / "b")},
            ],
        }
    )
    lineup = build_lineup(config)
    player = MockPlayer()
    om = OverlayManager(player, config, clock=FakeClock())
    om.show_admin_browser(lineup, highlight_number=4)
    ass = om._player.overlays[5]
    assert "Select a channel" in ass
    assert "CH 04  Bugs" in ass
    assert "CH 03  Arthur" in ass
    assert "3 eps" in ass  # Bugs' episode count label
    assert "0 eps" in ass  # Arthur has no folder in this test, so 0 episodes
    assert "mute select" in ass
    # only the highlighted tile gets a selection ring drawn (\3c is the
    # outline-color tag used only by _outline_rect, not by plain text labels)
    assert ass.count("\\3c") == 1


def test_admin_browser_draws_continue_watching_row(tmp_path):
    from nostalgiabox.channel import build_lineup
    from nostalgiabox.watch_state import ContinueEntry

    make_show(tmp_path, "b", 3)
    config = config_from_dict(
        {
            "shuffle_seed": 1,
            "channels": [{"number": 4, "name": "Bugs", "path": str(tmp_path / "b")}],
        }
    )
    lineup = build_lineup(config)
    entry = ContinueEntry(
        channel_number=4,
        channel_name="Bugs",
        episode_path=tmp_path / "b" / "b_ep01.mp4",
        title="Bugs Ep 1",
        resume_position=200.0,
        minutes_left=12,
        last_played=1000.0,
    )
    player = MockPlayer()
    om = OverlayManager(player, config, clock=FakeClock())
    om.show_admin_browser(lineup, highlight_number=4, continue_entries=[entry], continue_index=0)
    ass = player.overlays[5]
    assert "Continue Watching" in ass
    assert "Bugs - Bugs Ep 1" in ass
    assert "12 min left" in ass
    # the continue-row entry is selected, so the grid tile itself must not
    # also show as highlighted - only one outline ring total.
    assert ass.count("\\3c") == 1


def test_admin_browser_without_continue_entries_omits_the_row(tmp_path):
    from nostalgiabox.channel import build_lineup

    make_show(tmp_path, "b", 3)
    config = config_from_dict(
        {
            "shuffle_seed": 1,
            "channels": [{"number": 4, "name": "Bugs", "path": str(tmp_path / "b")}],
        }
    )
    lineup = build_lineup(config)
    player = MockPlayer()
    om = OverlayManager(player, config, clock=FakeClock())
    om.show_admin_browser(lineup, highlight_number=4)
    ass = player.overlays[5]
    assert "Continue Watching" not in ass
    # grid tile highlighting is unaffected by the (absent) continue row.
    assert ass.count("\\3c") == 1


def test_admin_browser_and_panel_share_overlay_slot(tmp_path):
    from nostalgiabox.channel import build_lineup

    config = _config(tmp_path)
    lineup = build_lineup(config)
    player = MockPlayer()
    om = OverlayManager(player, config, clock=FakeClock())
    om.show_admin_browser(lineup, highlight_number=3)
    assert "Select a channel" in player.overlays[5]
    om.show_admin_panel(lineup, paused=False)
    assert "Select a channel" not in player.overlays[5]  # replaced, not stacked
    om.clear_admin_panel()
    assert 5 not in player.overlays


def test_admin_episode_list_highlights_selected_episode(tmp_path):
    from nostalgiabox.channel import build_lineup

    make_show(tmp_path, "b", 3)
    config = config_from_dict(
        {
            "shuffle_seed": 1,
            "channels": [{"number": 4, "name": "Bugs", "path": str(tmp_path / "b")}],
        }
    )
    lineup = build_lineup(config)
    channel = next(c for c in lineup if c.number == 4)
    player = MockPlayer()
    om = OverlayManager(player, config, clock=FakeClock())
    om.show_admin_episode_list(channel, highlight_index=1)
    ass = player.overlays[5]
    assert "Bugs" in ass
    assert "Select an episode" in ass
    assert "1.  " in ass and "2.  " in ass and "3.  " in ass
    assert "power back" in ass


def test_admin_episode_list_scrolls_to_keep_selection_visible(tmp_path):
    from nostalgiabox.channel import build_lineup
    from nostalgiabox.overlay import _EPISODE_VISIBLE_ROWS

    make_show(tmp_path, "b", 41)
    config = config_from_dict(
        {
            "shuffle_seed": 1,
            "channels": [{"number": 4, "name": "Bluey", "path": str(tmp_path / "b")}],
        }
    )
    lineup = build_lineup(config)
    channel = next(c for c in lineup if c.number == 4)
    assert len(channel.episodes) == 41
    player = MockPlayer()
    om = OverlayManager(player, config, clock=FakeClock())

    # Near the top: no "more above" hint, but there are more below.
    om.show_admin_episode_list(channel, highlight_index=0)
    ass = player.overlays[5]
    assert "1.  " in ass
    assert "\u25b2" not in ass  # nothing above yet
    assert "\u25bc" in ass  # more below
    assert "(1 of 41)" in ass

    # Deep in the middle: the selected row's own label must actually be
    # drawn (this is the bug that was reported - the cursor kept moving
    # but the screen never scrolled to follow it).
    om.show_admin_episode_list(channel, highlight_index=30)
    ass = player.overlays[5]
    assert "31.  " in ass
    assert "\u25b2" in ass  # scrolled past the top now
    assert "(31 of 41)" in ass

    # Near the bottom: no "more below" hint since row 41 is the last one.
    om.show_admin_episode_list(channel, highlight_index=40)
    ass = player.overlays[5]
    assert "41.  " in ass
    assert "\u25b2" in ass
    assert "\u25bc" not in ass
    assert "(41 of 41)" in ass

    # Sanity: the window never shows more rows than fit on screen.
    row_lines = [ln for ln in ass.split("\n") if ln.split("}")[-1][:1].isdigit()]
    assert len(row_lines) <= _EPISODE_VISIBLE_ROWS


def _config_with_games(tmp_path, *, snes_roms=3, arthur_eps=2):
    make_show(tmp_path, "a", arthur_eps)
    make_show(tmp_path, "snes", snes_roms, ext=".sfc")
    return config_from_dict(
        {
            "shuffle_seed": 1,
            "channels": [{"number": 3, "name": "Arthur", "path": str(tmp_path / "a")}],
            "games": {
                "systems": [
                    {"name": "SNES", "path": str(tmp_path / "snes"), "core": "core.so", "extensions": [".sfc"]}
                ]
            },
        }
    )


def test_admin_browser_shows_game_systems_alongside_channels(tmp_path):
    from nostalgiabox.channel import build_game_channels, build_lineup

    config = _config_with_games(tmp_path, snes_roms=3, arthur_eps=2)
    lineup = build_lineup(config)
    games = build_game_channels(config)
    tiles = list(lineup) + games
    player = MockPlayer()
    om = OverlayManager(player, config, clock=FakeClock())
    om.show_admin_browser(tiles, highlight_number=games[0].number)
    ass = player.overlays[5]
    assert "SNES" in ass
    assert "3 games" in ass    # game systems count in "games", not "eps"
    assert "2 eps" in ass      # real channels are unaffected
    assert "Shows" in ass and "Games" in ass  # section swimlane labels


def test_admin_browser_omits_games_section_label_when_there_are_no_games(tmp_path):
    from nostalgiabox.channel import build_lineup

    make_show(tmp_path, "a", 2)
    config = config_from_dict(
        {
            "shuffle_seed": 1,
            "channels": [{"number": 3, "name": "Arthur", "path": str(tmp_path / "a")}],
        }
    )
    lineup = build_lineup(config)
    player = MockPlayer()
    om = OverlayManager(player, config, clock=FakeClock())
    om.show_admin_browser(lineup, highlight_number=3)
    ass = player.overlays[5]
    assert "Shows" in ass
    assert "Games" not in ass


def test_admin_episode_list_says_select_a_game_for_game_channels(tmp_path):
    from nostalgiabox.channel import build_game_channels

    config = _config_with_games(tmp_path, snes_roms=2)
    system = build_game_channels(config)[0]
    player = MockPlayer()
    om = OverlayManager(player, config, clock=FakeClock())
    om.show_admin_episode_list(system, highlight_index=0)
    ass = player.overlays[5]
    assert "Select a game" in ass
    assert "Select an episode" not in ass
    assert "SNES" in ass


def test_admin_episode_list_says_select_an_episode_for_show_channels(tmp_path):
    from nostalgiabox.channel import build_lineup

    config = _config_with_games(tmp_path)
    channel = next(c for c in build_lineup(config) if c.number == 3)
    player = MockPlayer()
    om = OverlayManager(player, config, clock=FakeClock())
    om.show_admin_episode_list(channel, highlight_index=0)
    assert "Select an episode" in player.overlays[5]


def test_admin_episode_list_and_browser_share_overlay_slot(tmp_path):
    from nostalgiabox.channel import build_lineup

    make_show(tmp_path, "b", 2)
    config = config_from_dict(
        {
            "shuffle_seed": 1,
            "channels": [{"number": 4, "name": "Bugs", "path": str(tmp_path / "b")}],
        }
    )
    lineup = build_lineup(config)
    channel = next(c for c in lineup if c.number == 4)
    player = MockPlayer()
    om = OverlayManager(player, config, clock=FakeClock())
    om.show_admin_browser(lineup, highlight_number=4)
    assert "Select a channel" in player.overlays[5]
    om.show_admin_episode_list(channel, highlight_index=0)
    assert "Select a channel" not in player.overlays[5]  # replaced, not stacked
    assert "Select an episode" in player.overlays[5]
    om.clear_admin_panel()
    assert 5 not in player.overlays
