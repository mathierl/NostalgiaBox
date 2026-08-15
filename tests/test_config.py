import pytest

from nostalgiabox.config import (
    ConfigError,
    config_from_dict,
    load_config,
)
from tests.helpers import make_show


def test_explicit_channels(tmp_path):
    make_show(tmp_path, "dragon-tales", 3)
    make_show(tmp_path, "arthur", 2)
    data = {
        "channels": [
            {"number": 2, "name": "Dragon Tales", "path": str(tmp_path / "dragon-tales")},
            {"number": 3, "name": "Arthur", "path": str(tmp_path / "arthur")},
        ]
    }
    cfg = config_from_dict(data)
    assert cfg.channel_numbers() == [2, 3]
    assert cfg.channels[0].name == "Dragon Tales"
    assert cfg.tune_in == "random"  # default


def test_channel_number_and_name_defaults(tmp_path):
    make_show(tmp_path, "magic_school_bus", 1)
    data = {"channels": [{"path": str(tmp_path / "magic_school_bus")}]}
    cfg = config_from_dict(data)
    # number defaults to index+2, name derived + prettified from folder
    assert cfg.channels[0].number == 2
    assert cfg.channels[0].name == "Magic School Bus"


def test_media_root_autodiscovery(tmp_path):
    make_show(tmp_path, "arthur", 1)
    make_show(tmp_path, "rugrats", 1)
    make_show(tmp_path, "dragon tales", 1)
    (tmp_path / ".hidden").mkdir()
    cfg = config_from_dict({"media_root": str(tmp_path)})
    # alphabetical order, numbered from 2, hidden folder ignored
    assert [(c.number, c.name) for c in cfg.channels] == [
        (2, "Arthur"),
        (3, "Dragon Tales"),
        (4, "Rugrats"),
    ]


def test_autodiscovery_custom_first_number(tmp_path):
    make_show(tmp_path, "arthur", 1)
    cfg = config_from_dict({"media_root": str(tmp_path), "first_channel_number": 7})
    assert cfg.channels[0].number == 7


def test_duplicate_channel_numbers_rejected(tmp_path):
    make_show(tmp_path, "a", 1)
    make_show(tmp_path, "b", 1)
    data = {
        "channels": [
            {"number": 5, "name": "A", "path": str(tmp_path / "a")},
            {"number": 5, "name": "B", "path": str(tmp_path / "b")},
        ]
    }
    with pytest.raises(ConfigError, match="duplicate channel number"):
        config_from_dict(data)


def test_missing_channels_and_media_root():
    with pytest.raises(ConfigError, match="either 'channels' or 'media_root'"):
        config_from_dict({})


def test_bad_tune_in_mode(tmp_path):
    make_show(tmp_path, "a", 1)
    data = {"tune_in": "nonsense", "channels": [{"path": str(tmp_path / "a")}]}
    with pytest.raises(ConfigError, match="tune_in"):
        config_from_dict(data)


def test_volume_and_durations_clamped(tmp_path):
    make_show(tmp_path, "a", 1)
    data = {
        "initial_volume": 500,
        "volume_step": 0,
        "transition_duration": -3,
        "channels": [{"path": str(tmp_path / "a")}],
    }
    cfg = config_from_dict(data)
    assert cfg.initial_volume == 100
    assert cfg.volume_step == 1
    assert cfg.transition_duration == 0.0


def test_video_extensions_normalised(tmp_path):
    make_show(tmp_path, "a", 1)
    data = {
        "video_extensions": ["mp4", ".MKV"],
        "channels": [{"path": str(tmp_path / "a")}],
    }
    cfg = config_from_dict(data)
    assert cfg.video_extensions == (".mp4", ".mkv")


def test_ui_and_crt_defaults(tmp_path):
    make_show(tmp_path, "a", 1)
    cfg = config_from_dict({"channels": [{"path": str(tmp_path / "a")}]})
    assert cfg.ui.font == "VT323"
    assert cfg.ui.color == "#4DFF5A"
    assert cfg.crt.enabled is True
    assert cfg.force_4_3 is False   # shows keep their native aspect by default
    assert cfg.start_offset_min == 6.0
    assert cfg.start_offset_max == 10.0
    assert cfg.transition_effect == "none"
    assert cfg.transition_duration == 0.4
    assert cfg.bridge_seconds == 0.8


def test_start_offset_forms(tmp_path):
    make_show(tmp_path, "a", 1)
    base = {"channels": [{"path": str(tmp_path / "a")}]}
    # single number -> min == max
    c1 = config_from_dict({**base, "start_offset": 8})
    assert (c1.start_offset_min, c1.start_offset_max) == (8.0, 8.0)
    # [min, max] list
    c2 = config_from_dict({**base, "start_offset": [6, 10]})
    assert (c2.start_offset_min, c2.start_offset_max) == (6.0, 10.0)
    # explicit keys, and min/max get ordered
    c3 = config_from_dict({**base, "start_offset_min": 10, "start_offset_max": 6})
    assert (c3.start_offset_min, c3.start_offset_max) == (10.0, 10.0)


def test_ui_and_crt_overrides(tmp_path):
    make_show(tmp_path, "a", 1)
    cfg = config_from_dict(
        {
            "channels": [{"path": str(tmp_path / "a")}],
            "ui": {"font": "Press Start 2P", "color": "00FF00", "glow": False},
            "crt": {"enabled": False, "curvature": 0.2, "scanlines": False},
        }
    )
    assert cfg.ui.font == "Press Start 2P"
    assert cfg.ui.color == "#00FF00"  # normalised with leading '#'
    assert cfg.ui.glow is False
    assert cfg.crt.enabled is False
    assert cfg.crt.curvature == 0.2
    assert cfg.crt.scanlines is False


def test_crt_values_clamped(tmp_path):
    make_show(tmp_path, "a", 1)
    cfg = config_from_dict(
        {
            "channels": [{"path": str(tmp_path / "a")}],
            "crt": {"curvature": 5.0, "vignette": -1},
        }
    )
    assert cfg.crt.curvature == 0.5   # clamped to max
    assert cfg.crt.vignette == 0.0    # clamped to min


def test_bad_transition_rejected(tmp_path):
    make_show(tmp_path, "a", 1)
    with pytest.raises(ConfigError, match="transition"):
        config_from_dict(
            {"channels": [{"path": str(tmp_path / "a")}], "transition": "sparkles"}
        )


def test_bad_color_rejected(tmp_path):
    make_show(tmp_path, "a", 1)
    with pytest.raises(ConfigError, match="ui.color"):
        config_from_dict(
            {"channels": [{"path": str(tmp_path / "a")}], "ui": {"color": "greenish"}}
        )


def test_load_config_from_file(tmp_path):
    make_show(tmp_path, "arthur", 1)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text(
        "channels:\n"
        f"  - path: {tmp_path / 'arthur'}\n"
        "    name: Arthur\n"
        "    number: 3\n"
        "tune_in: resume\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.tune_in == "resume"
    assert cfg.channels[0].number == 3


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_relative_paths_resolved_against_config_dir(tmp_path):
    make_show(tmp_path, "arthur", 1)
    cfg_file = tmp_path / "config.yaml"
    cfg_file.write_text("channels:\n  - path: arthur\n    name: Arthur\n")
    cfg = load_config(cfg_file)
    assert cfg.channels[0].path == tmp_path / "arthur"


def test_admin_mode_defaults(tmp_path):
    make_show(tmp_path, "a", 1)
    data = {"channels": [{"path": str(tmp_path / "a")}]}
    cfg = config_from_dict(data)
    assert cfg.admin_mode_enabled is True
    assert cfg.admin_hold_seconds == 3.0


def test_admin_mode_can_be_disabled_and_hold_seconds_clamped(tmp_path):
    make_show(tmp_path, "a", 1)
    data = {
        "admin_mode_enabled": False,
        "admin_hold_seconds": 100,
        "channels": [{"path": str(tmp_path / "a")}],
    }
    cfg = config_from_dict(data)
    assert cfg.admin_mode_enabled is False
    assert cfg.admin_hold_seconds == 15.0  # clamped to the max


# -- games (admin mode arcade, UKE-28) --------------------------------------


def _base_channels(tmp_path):
    make_show(tmp_path, "dragon", 1)
    return {"channels": [{"number": 2, "name": "Dragon Tales", "path": str(tmp_path / "dragon")}]}


def test_games_section_parsed(tmp_path):
    make_show(tmp_path, "snes", 2, ext=".sfc")
    make_show(tmp_path, "ps1", 1, ext=".cue")
    data = {
        **_base_channels(tmp_path),
        "games": {
            "systems": [
                {
                    "name": "SNES",
                    "path": str(tmp_path / "snes"),
                    "core": "/cores/snes9x.so",
                    "extensions": [".sfc"],
                },
                {
                    "name": "PS1",
                    "path": str(tmp_path / "ps1"),
                    "core": "/cores/pcsx.so",
                    "extensions": ["cue"],  # no leading dot - should be normalised
                },
            ]
        },
    }
    cfg = config_from_dict(data)
    assert [g.name for g in cfg.games] == ["SNES", "PS1"]
    assert [g.kind for g in cfg.games] == ["game", "game"]
    # numbers continue on from the last real channel (2) by default
    assert [g.number for g in cfg.games] == [3, 4]
    assert cfg.games[0].core == "/cores/snes9x.so"
    assert cfg.games[0].extensions == (".sfc",)
    assert cfg.games[1].extensions == (".cue",)


def test_games_section_explicit_number(tmp_path):
    make_show(tmp_path, "snes", 1, ext=".sfc")
    data = {
        **_base_channels(tmp_path),
        "games": {
            "systems": [
                {
                    "number": 50,
                    "name": "SNES",
                    "path": str(tmp_path / "snes"),
                    "core": "core.so",
                    "extensions": [".sfc"],
                }
            ]
        },
    }
    cfg = config_from_dict(data)
    assert cfg.games[0].number == 50


def test_no_games_section_defaults_to_empty(tmp_path):
    cfg = config_from_dict(_base_channels(tmp_path))
    assert cfg.games == ()


def test_games_missing_core_rejected(tmp_path):
    make_show(tmp_path, "snes", 1, ext=".sfc")
    data = {
        **_base_channels(tmp_path),
        "games": {"systems": [{"name": "SNES", "path": str(tmp_path / "snes"), "extensions": [".sfc"]}]},
    }
    with pytest.raises(ConfigError, match="core"):
        config_from_dict(data)


def test_games_missing_extensions_rejected(tmp_path):
    make_show(tmp_path, "snes", 1, ext=".sfc")
    data = {
        **_base_channels(tmp_path),
        "games": {"systems": [{"name": "SNES", "path": str(tmp_path / "snes"), "core": "core.so"}]},
    }
    with pytest.raises(ConfigError, match="extensions"):
        config_from_dict(data)


def test_games_missing_path_rejected(tmp_path):
    data = {
        **_base_channels(tmp_path),
        "games": {"systems": [{"name": "SNES", "core": "core.so", "extensions": [".sfc"]}]},
    }
    with pytest.raises(ConfigError, match="path"):
        config_from_dict(data)


def test_game_and_channel_sharing_a_number_rejected(tmp_path):
    make_show(tmp_path, "snes", 1, ext=".sfc")
    data = {
        **_base_channels(tmp_path),  # channel number 2
        "games": {
            "systems": [
                {"number": 2, "name": "SNES", "path": str(tmp_path / "snes"), "core": "core.so", "extensions": [".sfc"]}
            ]
        },
    }
    with pytest.raises(ConfigError, match="duplicate channel number"):
        config_from_dict(data)


def test_game_channel_config_rejects_bad_kind(tmp_path):
    from nostalgiabox.config import ChannelConfig

    with pytest.raises(ConfigError, match="kind"):
        ChannelConfig(number=1, name="Bad", path=tmp_path, kind="movie")


def test_game_channel_config_requires_core_and_extensions(tmp_path):
    from nostalgiabox.config import ChannelConfig

    with pytest.raises(ConfigError, match="core"):
        ChannelConfig(number=1, name="SNES", path=tmp_path, kind="game", extensions=(".sfc",))
    with pytest.raises(ConfigError, match="extensions"):
        ChannelConfig(number=1, name="SNES", path=tmp_path, kind="game", core="core.so")


def test_gpu_context_defaults_to_drm_and_can_be_disabled(tmp_path):
    make_show(tmp_path, "a", 1)
    data = {"channels": [{"path": str(tmp_path / "a")}]}
    cfg = config_from_dict(data)
    assert cfg.gpu_context == "drm"

    data["gpu_context"] = None
    cfg = config_from_dict(data)
    assert cfg.gpu_context is None

    data["gpu_context"] = "x11"
    cfg = config_from_dict(data)
    assert cfg.gpu_context == "x11"
