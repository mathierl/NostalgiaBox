from PIL import Image

from nostalgiabox import thumbnails
from nostalgiabox.channel import build_lineup
from nostalgiabox.config import config_from_dict
from tests.helpers import make_show


def _lineup(tmp_path, *, shows):
    """Build a real ChannelLineup with ``shows`` = [(number, name, episode_count), ...]."""
    channels = []
    for number, name, count in shows:
        make_show(tmp_path, name, count)
        channels.append({"number": number, "name": name, "path": str(tmp_path / name)})
    config = config_from_dict({"shuffle_seed": 1, "channels": channels})
    return list(build_lineup(config))


def test_admin_section_layout_fixed_tile_size_regardless_of_count(tmp_path):
    # UKE-29: tiles no longer shrink to cram every one into a single row -
    # a section with 2 shows and one with 5 shows get the *same* tile size.
    few = _lineup(tmp_path, shows=[(2, "a", 1), (3, "b", 1)])
    many_dir = tmp_path / "many"
    many_dir.mkdir()
    many = _lineup(many_dir, shows=[(2 + i, f"s{i}", 1) for i in range(5)])

    few_sections = thumbnails.admin_section_layout(few)
    many_sections = thumbnails.admin_section_layout(many)
    assert few_sections[0].tiles[0].w == many_sections[0].tiles[0].w


def test_admin_section_layout_wraps_a_long_section_onto_multiple_rows(tmp_path):
    channels = _lineup(tmp_path, shows=[(2 + i, f"s{i}", 1) for i in range(10)])
    section = thumbnails.admin_section_layout(channels)[0]
    ys = sorted({tile.y for tile in section.tiles})
    assert len(ys) > 1  # more than one distinct row of tiles
    # every row is populated left-to-right starting at the same left margin
    for y in ys:
        row_tiles = [t for t in section.tiles if t.y == y]
        assert row_tiles[0].x == min(t.x for t in section.tiles)


def test_admin_section_layout_matches_overlay_canvas_width():
    # thumbnails.GRID_W must equal nostalgiabox.overlay.CANVAS_W - the
    # background image is only full-width if the two agree (see both
    # modules' docstrings).
    from nostalgiabox.overlay import CANVAS_H, CANVAS_W

    assert thumbnails.GRID_W == CANVAS_W
    assert thumbnails.GRID_H == CANVAS_H


def test_sections_bottom_grows_with_more_wrapped_rows(tmp_path):
    few = _lineup(tmp_path, shows=[(2, "a", 1)])
    many_dir = tmp_path / "many"
    many_dir.mkdir()
    many = _lineup(many_dir, shows=[(2 + i, f"s{i}", 1) for i in range(10)])
    assert thumbnails.sections_bottom(many) > thumbnails.sections_bottom(few)


def test_sections_bottom_with_no_tiles_is_just_the_header(tmp_path):
    assert thumbnails.sections_bottom([]) == thumbnails.GRID_HEADER_H


def test_scrollable_content_height_reserves_room_for_evergreen_rows(tmp_path):
    # Three evergreen rows now (UKE-29 v2): Insights, Adult Mode, and Open
    # RetroArch - the last one's space is reserved even while Adult Mode is
    # off and it isn't shown (see scrollable_content_height's docstring).
    channels = _lineup(tmp_path, shows=[(2, "a", 1)])
    bottom = thumbnails.sections_bottom(channels)
    expected = bottom + 3 * (thumbnails.EVERGREEN_ROW_H + thumbnails.EVERGREEN_GAP_ABOVE) + thumbnails.GRID_FOOTER_H
    assert thumbnails.scrollable_content_height(channels) == expected


def test_section_bounds_returns_none_for_a_missing_section(tmp_path):
    channels = _lineup(tmp_path, shows=[(2, "a", 1)])
    assert thumbnails.section_bounds(channels, "Nonexistent") is None
    bounds = thumbnails.section_bounds(channels, "Shows")
    assert bounds is not None
    assert bounds[0] < bounds[1]


def test_tile_bounds_includes_label_height(tmp_path):
    channels = _lineup(tmp_path, shows=[(2, "a", 1)])
    tile = thumbnails.admin_section_layout(channels)[0].tiles[0]
    top, bottom = thumbnails.tile_bounds(tile)
    assert top == tile.y
    assert bottom == tile.y + tile.h + 58  # thumbnails._GRID_LABEL_H


def test_clamp_scroll_stays_within_bounds(tmp_path):
    channels = _lineup(tmp_path, shows=[(2 + i, f"s{i}", 1) for i in range(10)])
    assert thumbnails.clamp_scroll(channels, -50) == 0
    assert thumbnails.clamp_scroll(channels, 10**9) == thumbnails.scrollable_content_height(channels) - thumbnails.GRID_H
    assert thumbnails.clamp_scroll([], 500) == 0  # nothing to scroll at all


def test_crop_viewport_pins_the_header_and_scrolls_the_body(tmp_path):
    # A vertical gradient so different crop offsets are distinguishable by
    # their actual pixel content, not just by trusting the offset math.
    height = 1400
    column = Image.new("L", (1, height))
    column.putdata([y % 256 for y in range(height)])
    source = column.resize((thumbnails.GRID_W, height)).convert("RGB")
    source_path = tmp_path / "source.jpg"
    source.save(source_path, quality=95)

    out_a = tmp_path / "viewport_a.jpg"
    out_b = tmp_path / "viewport_b.jpg"
    thumbnails.crop_viewport(source_path, 0, out_a)
    thumbnails.crop_viewport(source_path, 200, out_b)

    with Image.open(out_a) as a, Image.open(out_b) as b:
        assert a.size == (thumbnails.GRID_W, thumbnails.GRID_H)
        assert b.size == (thumbnails.GRID_W, thumbnails.GRID_H)
        # The pinned header band (top GRID_HEADER_H rows) must be identical
        # regardless of scroll_y - it always mirrors the source's own top,
        # unscrolled rows (see the module docstring / crop_viewport). Stop
        # a little short of the header/body boundary: JPEG's lossy block
        # encoding can nudge a pixel or two right at that edge even when
        # the underlying source rows are byte-identical.
        margin = 20
        header_a = a.crop((0, 0, thumbnails.GRID_W, thumbnails.GRID_HEADER_H - margin)).tobytes()
        header_b = b.crop((0, 0, thumbnails.GRID_W, thumbnails.GRID_HEADER_H - margin)).tobytes()
        assert header_a == header_b
        # But the body (everything below the header) must differ once
        # scrolled - the whole point of scroll_y.
        body_a = a.crop((0, thumbnails.GRID_HEADER_H, thumbnails.GRID_W, thumbnails.GRID_H)).tobytes()
        body_b = b.crop((0, thumbnails.GRID_HEADER_H, thumbnails.GRID_W, thumbnails.GRID_H)).tobytes()
        assert body_a != body_b


def test_crop_viewport_clamps_past_the_bottom_of_the_source(tmp_path):
    height = 800
    source = Image.new("RGB", (thumbnails.GRID_W, height), (10, 10, 10))
    source_path = tmp_path / "source.jpg"
    source.save(source_path, quality=88)

    out_path = tmp_path / "viewport.jpg"
    result = thumbnails.crop_viewport(source_path, 10**6, out_path)
    assert result is not None
    with Image.open(out_path) as img:
        assert img.size == (thumbnails.GRID_W, thumbnails.GRID_H)


def test_compose_show_grid_is_taller_when_content_overflows_one_screen(tmp_path):
    few = _lineup(tmp_path, shows=[(2, "a", 1)])
    many_dir = tmp_path / "many"
    many_dir.mkdir()
    many = _lineup(many_dir, shows=[(2 + i, f"s{i}", 1) for i in range(10)])

    out_few = tmp_path / "few.jpg"
    out_many = tmp_path / "many.jpg"
    thumbnails.compose_show_grid(few, {}, out_few)
    thumbnails.compose_show_grid(many, {}, out_many)

    # "few" is exactly as tall as the content needs (which may itself be a
    # touch over one screen now that three evergreen rows' worth of space -
    # Insights, Adult Mode, Open RetroArch - is always reserved, UKE-29 v2);
    # "many" is taller still, since it also has to fit the wrapped tile rows.
    with Image.open(out_few) as img:
        assert img.size == (thumbnails.GRID_W, max(thumbnails.GRID_H, thumbnails.scrollable_content_height(few)))
    with Image.open(out_many) as img:
        assert img.size[1] > thumbnails.scrollable_content_height(few)
        assert img.size[0] == thumbnails.GRID_W
