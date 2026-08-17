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


def test_adult_mode_status_is_a_transient_message_not_a_persistent_overlay(tmp_path):
    # UKE-29: the old always-on-screen corner panel (every channel +
    # PAUSED/ADMIN, glued to the picture until admin mode was fully exited)
    # was reported as a bug - Adult Mode's on/off feedback is deliberately
    # just a brief message, sharing the same overlay slot/expiry as every
    # other OSD message (seek, volume, "no signal", ...), not a new
    # persistent one.
    clock = FakeClock()
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=clock)
    om.show_adult_mode_status(on=True)
    assert "Adult Mode" in player.overlays[4] and "ON" in player.overlays[4]
    clock.advance(10_000)
    om.tick()
    assert 4 not in player.overlays  # expires like any other message

    om.show_adult_mode_status(on=False)
    assert "OFF" in player.overlays[4]


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
    ass = player.overlays[5]
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


def test_admin_browser_draws_evergreen_insights_row(tmp_path):
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

    # Not selected: no extra outline beyond the grid tile's own.
    om.show_admin_browser(lineup, highlight_number=3, insights_selected=False)
    ass = player.overlays[5]
    assert "Watch Insights" in ass
    assert ass.count("\\3c") == 1

    # Selected: the grid tile is no longer highlighted, only the Insights row.
    om.show_admin_browser(lineup, highlight_number=3, insights_selected=True)
    ass = player.overlays[5]
    assert "Watch Insights" in ass
    assert ass.count("\\3c") == 1


def test_admin_browser_draws_evergreen_adult_mode_row(tmp_path):
    # UKE-29: the second evergreen row, right below Insights - reflects
    # on/off state via its label text, and highlights independently.
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

    om.show_admin_browser(lineup, highlight_number=3, adult_mode=False)
    ass = player.overlays[5]
    assert "Adult Mode: OFF" in ass

    om.show_admin_browser(lineup, highlight_number=3, adult_mode=True, adult_toggle_selected=True)
    ass = player.overlays[5]
    assert "Adult Mode: ON" in ass
    # the toggle row is selected, so the grid tile must not also be
    # highlighted - only one outline ring total.
    assert ass.count("\\3c") == 1


def test_admin_browser_and_episode_list_backdrops_span_the_full_canvas_width(tmp_path):
    # UKE-29 real-hardware feedback: the admin UI used to be confined to the
    # 960px-wide 4:3 "tube" every actual show plays in, wasting a quarter of
    # a widescreen TV. It's now full-width, matching the whole 1280-wide
    # canvas - the grid's tiles/labels and the episode-list/Insights opaque
    # backdrop should both reach edge to edge, not stop at the old 4:3 inset.
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
    xs = _all_x_positions(player.overlays[5])
    # positions comfortably beyond the old 960-wide frame's right edge (1120)
    assert any(x > _FRAME_X1 for x in xs)

    channel = next(c for c in lineup if c.number == 3)
    om.show_admin_episode_list(channel, highlight_index=0)
    ass = player.overlays[5]
    # the backdrop rectangle is drawn "m 0 0 l <w> 0 ..." at x=0 - a plain
    # rectangle spanning the full 1280 width, not the old 960-wide inset.
    assert "l 1280 0" in ass


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
    assert "▲" not in ass  # nothing above yet
    assert "▼" in ass  # more below
    assert "(1 of 41)" in ass

    # Deep in the middle: the selected row's own label must actually be
    # drawn (this is the bug that was reported - the cursor kept moving
    # but the screen never scrolled to follow it).
    om.show_admin_episode_list(channel, highlight_index=30)
    ass = player.overlays[5]
    assert "31.  " in ass
    assert "▲" in ass  # scrolled past the top now
    assert "(31 of 41)" in ass

    # Near the bottom: no "more below" hint since row 41 is the last one.
    om.show_admin_episode_list(channel, highlight_index=40)
    ass = player.overlays[5]
    assert "41.  " in ass
    assert "▲" in ass
    assert "▼" not in ass
    assert "(41 of 41)" in ass

    # Sanity: the window never shows more rows than fit on screen.
    row_lines = [ln for ln in ass.split("\n") if ln.split("}")[-1][:1].isdigit()]
    assert len(row_lines) <= _EPISODE_VISIBLE_ROWS


def test_admin_episode_list_shows_watched_and_in_progress_markers(tmp_path):
    # UKE-29 regression: per-episode watched/in-progress state used to be
    # visible in the episode list (via the browser-era UI) and went missing
    # in the mpv+ASS revert - restored here via an optional watch_state arg.
    from nostalgiabox.channel import build_lineup
    from nostalgiabox.watch_state import WatchState

    make_show(tmp_path, "b", 3)
    config = config_from_dict(
        {
            "shuffle_seed": 1,
            "channels": [{"number": 4, "name": "Bugs", "path": str(tmp_path / "b")}],
        }
    )
    lineup = build_lineup(config)
    channel = next(c for c in lineup if c.number == 4)
    ws = WatchState(tmp_path / "watch_state.json")
    ws.mark_episode_watched(4, channel.config.path, channel.episodes[0])
    ws.record_episode_position(4, channel.config.path, channel.episodes[1], position=60.0, duration=600.0)

    player = MockPlayer()
    om = OverlayManager(player, config, clock=FakeClock())
    om.show_admin_episode_list(channel, highlight_index=0, watch_state=ws)
    ass = player.overlays[5]
    assert "Watched" in ass  # episode 1
    assert "10% watched" in ass  # episode 2: 60/600


def test_admin_episode_list_with_no_watch_state_shows_no_markers(tmp_path):
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
    om.show_admin_episode_list(channel, highlight_index=0)  # no watch_state
    ass = player.overlays[5]
    assert "Watched" not in ass and "% watched" not in ass


def test_admin_browser_scroll_y_shifts_tiles_but_not_the_header(tmp_path):
    # UKE-29: the header/Continue Watching row are pinned; everything below
    # (section labels, tiles, the evergreen rows) shifts up by scroll_y.
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

    om.show_admin_browser(lineup, highlight_number=3, scroll_y=0)
    unscrolled_ys = _all_y_positions(player.overlays[5])
    header_y = min(unscrolled_ys)  # "Select a channel" - always the topmost line

    om.show_admin_browser(lineup, highlight_number=3, scroll_y=50)
    scrolled_ys = _all_y_positions(player.overlays[5])
    assert min(scrolled_ys) == header_y  # header unmoved
    assert "CH 03  Arthur" in player.overlays[5]  # tile label still drawn (just shifted)


def _all_y_positions(ass: str):
    import re as _re

    return [int(m) for m in _re.findall(r"\\pos\(\d+,(-?\d+)\)", ass)]


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


def test_admin_episode_list_says_select_an_episode_for_show_channels(tmp_path):
    from nostalgiabox.channel import build_lineup

    config = _config(tmp_path)
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


# -- Insights screen (UKE-29) ------------------------------------------------
# No prior ASS version of this existed - it was originally built only for
# the (now-reverted) browser UI. See nostalgiabox.watch_state.insights_summary
# for how the data is rolled up.


def _empty_summary():
    from nostalgiabox.watch_state import InsightsSummary

    return InsightsSummary(
        channels=[], favorite=None, activity=[],
        total_watched_minutes=0, total_episodes_watched=0,
    )


def test_insights_empty_state(tmp_path):
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_admin_insights(_empty_summary())
    ass = player.overlays[5]
    assert "Nothing watched yet" in ass
    assert "power back" in ass


def test_insights_shows_totals_and_favorite(tmp_path):
    from nostalgiabox.watch_state import ChannelInsight, InsightsSummary

    fav = ChannelInsight(
        number=3, name="Arthur", watched_count=2, total_count=4,
        watched_minutes=42, last_played=1000.0,
    )
    summary = InsightsSummary(
        channels=[fav], favorite=fav, activity=[],
        total_watched_minutes=42, total_episodes_watched=2,
    )
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_admin_insights(summary, suggestions=["Bluey", "Bugs"])
    ass = player.overlays[5]
    assert "42" in ass  # total minutes watched
    assert "Favorite" in ass and "Arthur" in ass
    assert "Similar:" in ass and "Bluey" in ass and "Bugs" in ass
    assert "2 of 4" in ass  # Arthur's completion progress


def test_insights_shows_recent_activity(tmp_path):
    from nostalgiabox.watch_state import ActivityEntry, ChannelInsight, InsightsSummary

    fav = ChannelInsight(
        number=3, name="Arthur", watched_count=1, total_count=4,
        watched_minutes=10, last_played=1000.0,
    )
    activity = [
        ActivityEntry(
            channel_number=3, channel_name="Arthur",
            title="Sick as a Dog", when=1000.0, watched=True,
        )
    ]
    summary = InsightsSummary(
        channels=[fav], favorite=fav, activity=activity,
        total_watched_minutes=10, total_episodes_watched=1,
    )
    player = MockPlayer()
    om = OverlayManager(player, _config(tmp_path), clock=FakeClock())
    om.show_admin_insights(summary)
    ass = player.overlays[5]
    assert "Recent Activity" in ass
    assert "Sick as a Dog" in ass


def test_insights_and_browser_share_overlay_slot(tmp_path):
    from nostalgiabox.channel import build_lineup

    config = _config(tmp_path)
    lineup = build_lineup(config)
    player = MockPlayer()
    om = OverlayManager(player, config, clock=FakeClock())
    om.show_admin_browser(lineup, highlight_number=3)
    assert "Select a channel" in player.overlays[5]
    om.show_admin_insights(_empty_summary())
    assert "Select a channel" not in player.overlays[5]  # replaced, not stacked
    assert "Nothing watched yet" in player.overlays[5]
