import json

from nostalgiabox.channel import build_lineup, build_game_channels
from nostalgiabox.config import config_from_dict
from nostalgiabox.watch_state import (
    IN_PROGRESS_FLOOR,
    WATCHED_THRESHOLD,
    WatchState,
    continue_watching,
    insights_summary,
)
from tests.helpers import make_show


def test_unknown_episode_returns_defaults(tmp_path):
    ws = WatchState(tmp_path / "watch_state.json")
    root = tmp_path / "snes"
    state = ws.episode_state(2, root, root / "ep1.mp4")
    assert state.position == 0.0
    assert state.duration == 0.0
    assert state.watched is False
    assert state.in_progress is False


def test_record_episode_position_below_threshold_is_in_progress(tmp_path):
    ws = WatchState(tmp_path / "watch_state.json")
    root = tmp_path / "dragon"
    ep = root / "ep1.mp4"
    ws.record_episode_position(2, root, ep, position=440.0, duration=1320.0)  # 1/3 in
    state = ws.episode_state(2, root, ep)
    assert state.watched is False
    assert state.in_progress is True
    assert round(state.fraction, 2) == 0.33


def test_record_episode_position_crossing_threshold_marks_watched(tmp_path):
    ws = WatchState(tmp_path / "watch_state.json")
    root = tmp_path / "dragon"
    ep = root / "ep1.mp4"
    ws.record_episode_position(2, root, ep, position=1250.0, duration=1320.0)  # ~0.947
    state = ws.episode_state(2, root, ep)
    assert round(state.fraction, 3) >= WATCHED_THRESHOLD
    assert state.watched is True
    assert state.in_progress is False  # watched takes priority over in_progress


def test_position_just_above_floor_is_in_progress_not_ignored(tmp_path):
    ws = WatchState(tmp_path / "watch_state.json")
    root = tmp_path / "dragon"
    ep = root / "ep1.mp4"
    ws.record_episode_position(2, root, ep, position=IN_PROGRESS_FLOOR * 1320.0 + 1, duration=1320.0)
    assert ws.episode_state(2, root, ep).in_progress is True


def test_position_at_or_below_floor_is_not_in_progress(tmp_path):
    ws = WatchState(tmp_path / "watch_state.json")
    root = tmp_path / "dragon"
    ep = root / "ep1.mp4"
    ws.record_episode_position(2, root, ep, position=1.0, duration=1320.0)  # well under 2%
    assert ws.episode_state(2, root, ep).in_progress is False


def test_mark_episode_watched_sets_watched_and_full_position(tmp_path):
    ws = WatchState(tmp_path / "watch_state.json")
    root = tmp_path / "arthur"
    ep = root / "ep1.mp4"
    ws.mark_episode_watched(3, root, ep, duration=1320.0)
    state = ws.episode_state(3, root, ep)
    assert state.watched is True
    assert state.position == 1320.0
    assert state.duration == 1320.0


def test_duration_is_remembered_across_calls_once_known(tmp_path):
    # Simulates the real app: first call supplies a probed duration, later
    # calls (e.g. no re-probe) can pass 0 and the stored duration persists.
    ws = WatchState(tmp_path / "watch_state.json")
    root = tmp_path / "arthur"
    ep = root / "ep1.mp4"
    ws.record_episode_position(3, root, ep, position=100.0, duration=1320.0)
    ws.record_episode_position(3, root, ep, position=200.0, duration=0.0)
    state = ws.episode_state(3, root, ep)
    assert state.duration == 1320.0  # not clobbered by the later duration=0
    assert state.position == 200.0


def test_key_is_relative_to_channel_root_not_absolute_path(tmp_path):
    # Same relative filename under two different absolute roots (simulating
    # the media folder having moved) must resolve to the same watch state.
    ws = WatchState(tmp_path / "watch_state.json")
    root_a = tmp_path / "media_v1" / "arthur"
    root_b = tmp_path / "media_v2" / "arthur"
    ws.record_episode_position(3, root_a, root_a / "ep1.mp4", position=500.0, duration=1000.0)
    state = ws.episode_state(3, root_b, root_b / "ep1.mp4")
    assert state.position == 500.0


def test_games_tracked_as_boolean_play_count_not_position(tmp_path):
    ws = WatchState(tmp_path / "watch_state.json")
    root = tmp_path / "roms" / "snes"
    rom = root / "Donkey Kong Country.sfc"
    assert ws.game_state(5, root, rom).played is False

    ws.record_game_played(5, root, rom)
    state = ws.game_state(5, root, rom)
    assert state.played is True
    assert state.play_count == 1

    ws.record_game_played(5, root, rom)
    assert ws.game_state(5, root, rom).play_count == 2


def test_persists_across_instances(tmp_path):
    path = tmp_path / "watch_state.json"
    root = tmp_path / "dragon"
    rom_root = tmp_path / "roms" / "snes"

    ws1 = WatchState(path)
    ws1.record_episode_position(2, root, root / "ep1.mp4", position=300.0, duration=1320.0)
    ws1.record_game_played(5, rom_root, rom_root / "game.sfc")

    ws2 = WatchState(path)  # fresh instance, same file
    ep_state = ws2.episode_state(2, root, root / "ep1.mp4")
    game_state = ws2.game_state(5, rom_root, rom_root / "game.sfc")
    assert ep_state.position == 300.0
    assert game_state.played is True
    assert game_state.play_count == 1


def test_missing_file_starts_empty_without_error(tmp_path):
    ws = WatchState(tmp_path / "does_not_exist" / "watch_state.json")
    root = tmp_path / "a"
    assert ws.episode_state(2, root, root / "ep1.mp4").watched is False


def test_corrupt_file_starts_fresh_without_crashing(tmp_path):
    path = tmp_path / "watch_state.json"
    path.write_text("{ not valid json !!", encoding="utf-8")
    ws = WatchState(path)  # must not raise
    root = tmp_path / "a"
    assert ws.episode_state(2, root, root / "ep1.mp4").watched is False


def test_save_writes_atomically_no_stray_temp_files(tmp_path):
    path = tmp_path / "watch_state.json"
    root = tmp_path / "dragon"
    ws = WatchState(path)
    ws.record_episode_position(2, root, root / "ep1.mp4", position=10.0, duration=100.0)
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".watch_state-")]
    assert leftovers == []
    assert path.is_file()


def test_saved_file_shape(tmp_path):
    path = tmp_path / "watch_state.json"
    root = tmp_path / "dragon"
    rom_root = tmp_path / "roms" / "snes"
    ws = WatchState(path)
    ws.record_episode_position(2, root, root / "ep1.mp4", position=10.0, duration=100.0)
    ws.record_game_played(5, rom_root, rom_root / "game.sfc")

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["version"] == 1
    assert "2:ep1.mp4" in raw["shows"]
    assert "5:game.sfc" in raw["games"]


def test_all_episodes_and_all_games_return_snapshots(tmp_path):
    ws = WatchState(tmp_path / "watch_state.json")
    root = tmp_path / "dragon"
    rom_root = tmp_path / "roms" / "snes"
    ws.record_episode_position(2, root, root / "ep1.mp4", position=10.0, duration=100.0)
    ws.record_game_played(5, rom_root, rom_root / "game.sfc")
    assert len(ws.all_episodes()) == 1
    assert len(ws.all_games()) == 1


# -- continue_watching() (UKE-29) --------------------------------------------


def _lineup_with_games(tmp_path, *, dragon_eps=3, arthur_eps=3, snes_roms=2):
    make_show(tmp_path, "dragon", dragon_eps)
    make_show(tmp_path, "arthur", arthur_eps)
    make_show(tmp_path, "snes", snes_roms, ext=".sfc")
    config = config_from_dict(
        {
            "shuffle_seed": 1,
            "channels": [
                {"number": 2, "name": "Dragon Tales", "path": str(tmp_path / "dragon")},
                {"number": 3, "name": "Arthur", "path": str(tmp_path / "arthur")},
            ],
            "games": {
                "systems": [
                    {"name": "SNES", "path": str(tmp_path / "snes"), "core": "core.so", "extensions": [".sfc"]}
                ]
            },
        }
    )
    lineup = build_lineup(config)
    games = build_game_channels(config)
    return lineup, games


def test_continue_watching_returns_only_in_progress_episodes(tmp_path):
    lineup, _ = _lineup_with_games(tmp_path)
    ws = WatchState(tmp_path / "watch_state.json")
    dragon = next(c for c in lineup if c.number == 2)
    ws.record_episode_position(2, dragon.config.path, dragon.episodes[0], position=200.0, duration=1200.0)
    ws.mark_episode_watched(2, dragon.config.path, dragon.episodes[1], duration=1200.0)  # fully watched, excluded

    entries = continue_watching(list(lineup), ws)
    assert len(entries) == 1
    assert entries[0].channel_number == 2
    assert entries[0].episode_path == dragon.episodes[0]
    assert entries[0].minutes_left == round((1200.0 - 200.0) / 60)


def test_continue_watching_excludes_games(tmp_path):
    lineup, games = _lineup_with_games(tmp_path)
    ws = WatchState(tmp_path / "watch_state.json")
    snes = games[0]
    ws.record_game_played(snes.number, snes.config.path, snes.episodes[0])
    # games have no position, so even mixing them into the channel list must
    # never produce a continue-watching entry for one.
    entries = continue_watching(list(lineup) + games, ws)
    assert entries == []


def test_continue_watching_sorted_most_recent_first(tmp_path):
    lineup, _ = _lineup_with_games(tmp_path)
    ws = WatchState(tmp_path / "watch_state.json")
    dragon = next(c for c in lineup if c.number == 2)
    arthur = next(c for c in lineup if c.number == 3)
    ws.record_episode_position(2, dragon.config.path, dragon.episodes[0], position=200.0, duration=1200.0)
    ws.record_episode_position(3, arthur.config.path, arthur.episodes[0], position=200.0, duration=1200.0)

    entries = continue_watching(list(lineup), ws)
    assert [e.channel_number for e in entries] == [3, 2]  # arthur recorded last


def test_continue_watching_respects_limit(tmp_path):
    lineup, _ = _lineup_with_games(tmp_path, dragon_eps=3, arthur_eps=3)
    ws = WatchState(tmp_path / "watch_state.json")
    dragon = next(c for c in lineup if c.number == 2)
    arthur = next(c for c in lineup if c.number == 3)
    for ep in dragon.episodes:
        ws.record_episode_position(2, dragon.config.path, ep, position=200.0, duration=1200.0)
    for ep in arthur.episodes:
        ws.record_episode_position(3, arthur.config.path, ep, position=200.0, duration=1200.0)

    entries = continue_watching(list(lineup), ws, limit=2)
    assert len(entries) == 2


def test_continue_watching_with_no_watch_state_is_empty(tmp_path):
    lineup, _ = _lineup_with_games(tmp_path)
    assert continue_watching(list(lineup), None) == []


# -- insights_summary() (UKE-29) ---------------------------------------------


def test_insights_summary_with_no_watch_state_is_empty(tmp_path):
    lineup, games = _lineup_with_games(tmp_path)
    summary = insights_summary(list(lineup) + games, None)
    assert summary.channels == []
    assert summary.favorite is None
    assert summary.activity == []
    assert summary.total_watched_minutes == 0
    assert summary.total_episodes_watched == 0
    assert summary.total_games_played == 0


def test_insights_summary_per_channel_counts(tmp_path):
    lineup, games = _lineup_with_games(tmp_path, dragon_eps=3, arthur_eps=3, snes_roms=2)
    ws = WatchState(tmp_path / "watch_state.json")
    dragon = next(c for c in lineup if c.number == 2)
    snes = games[0]
    ws.mark_episode_watched(2, dragon.config.path, dragon.episodes[0], duration=600.0)
    ws.record_episode_position(2, dragon.config.path, dragon.episodes[1], position=300.0, duration=600.0)
    ws.record_game_played(snes.number, snes.config.path, snes.episodes[0])
    ws.record_game_played(snes.number, snes.config.path, snes.episodes[0])  # played twice

    summary = insights_summary(list(lineup) + games, ws)
    dragon_insight = next(c for c in summary.channels if c.number == 2)
    assert dragon_insight.watched_count == 1  # only the fully-watched one counts
    assert dragon_insight.total_count == 3
    assert dragon_insight.watched_minutes == 10 + 5  # 600s watched + 300s in-progress, in minutes
    assert dragon_insight.play_count == 0

    snes_insight = next(c for c in summary.channels if c.number == snes.number)
    assert snes_insight.watched_count == 1  # one rom played at least once
    assert snes_insight.watched_minutes is None  # no duration signal for games
    assert snes_insight.play_count == 2


def test_insights_summary_totals(tmp_path):
    lineup, games = _lineup_with_games(tmp_path, dragon_eps=2, arthur_eps=2, snes_roms=1)
    ws = WatchState(tmp_path / "watch_state.json")
    dragon = next(c for c in lineup if c.number == 2)
    arthur = next(c for c in lineup if c.number == 3)
    snes = games[0]
    ws.mark_episode_watched(2, dragon.config.path, dragon.episodes[0], duration=600.0)
    ws.mark_episode_watched(3, arthur.config.path, arthur.episodes[0], duration=1200.0)
    ws.record_game_played(snes.number, snes.config.path, snes.episodes[0])

    summary = insights_summary(list(lineup) + games, ws)
    assert summary.total_episodes_watched == 2
    assert summary.total_watched_minutes == 10 + 20
    assert summary.total_games_played == 1


def test_insights_summary_favorite_is_most_watched_by_minutes(tmp_path):
    lineup, _ = _lineup_with_games(tmp_path, dragon_eps=2, arthur_eps=2)
    ws = WatchState(tmp_path / "watch_state.json")
    dragon = next(c for c in lineup if c.number == 2)
    arthur = next(c for c in lineup if c.number == 3)
    ws.mark_episode_watched(2, dragon.config.path, dragon.episodes[0], duration=300.0)
    ws.mark_episode_watched(3, arthur.config.path, arthur.episodes[0], duration=3000.0)  # much more watched

    summary = insights_summary(list(lineup), ws)
    assert summary.favorite is not None
    assert summary.favorite.number == 3


def test_insights_summary_favorite_uses_play_count_for_games(tmp_path):
    lineup, games = _lineup_with_games(tmp_path, snes_roms=2)
    ws = WatchState(tmp_path / "watch_state.json")
    snes = games[0]
    # No shows touched at all - the only "favorite" candidate is the game system.
    ws.record_game_played(snes.number, snes.config.path, snes.episodes[0])
    ws.record_game_played(snes.number, snes.config.path, snes.episodes[1])

    summary = insights_summary(list(lineup) + games, ws)
    assert summary.favorite is not None
    assert summary.favorite.kind == "game"
    assert summary.favorite.number == snes.number


def test_insights_summary_untouched_channels_excluded_from_favorite(tmp_path):
    lineup, _ = _lineup_with_games(tmp_path)
    ws = WatchState(tmp_path / "watch_state.json")
    # Nothing ever recorded - untouched channels must not "win" favorite by
    # default (their watched_minutes/play_count would both be 0 too, but
    # last_played == 0 is the actual exclusion signal).
    summary = insights_summary(list(lineup), ws)
    assert summary.favorite is None


def test_insights_summary_activity_sorted_most_recent_first_and_limited(tmp_path):
    lineup, _ = _lineup_with_games(tmp_path, dragon_eps=3, arthur_eps=3)
    ws = WatchState(tmp_path / "watch_state.json")
    dragon = next(c for c in lineup if c.number == 2)
    arthur = next(c for c in lineup if c.number == 3)
    ws.record_episode_position(2, dragon.config.path, dragon.episodes[0], position=100.0, duration=1000.0)
    ws.record_episode_position(3, arthur.config.path, arthur.episodes[0], position=100.0, duration=1000.0)
    ws.record_episode_position(2, dragon.config.path, dragon.episodes[1], position=100.0, duration=1000.0)

    summary = insights_summary(list(lineup), ws, activity_limit=2)
    assert len(summary.activity) == 2
    assert summary.activity[0].channel_number == 2  # dragon episode 2 recorded last
    assert summary.activity[0].watched is False  # still in progress, not watched

    full = insights_summary(list(lineup), ws, activity_limit=10)
    assert len(full.activity) == 3


def test_insights_summary_activity_includes_games(tmp_path):
    lineup, games = _lineup_with_games(tmp_path, snes_roms=1)
    ws = WatchState(tmp_path / "watch_state.json")
    snes = games[0]
    ws.record_game_played(snes.number, snes.config.path, snes.episodes[0])

    summary = insights_summary(list(lineup) + games, ws)
    assert len(summary.activity) == 1
    assert summary.activity[0].kind == "game"
    assert summary.activity[0].watched is True
