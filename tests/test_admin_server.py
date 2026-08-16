"""Tests for the real local HTTP server behind the browser-based admin UI
(see nostalgiabox/admin_server.py). Uses port=0 (OS picks a free port) so
these can run in parallel/CI without clashing, and real HTTP requests
against it via urllib - this is the one piece of nostalgiabox that
legitimately needs a live socket to test meaningfully.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from nostalgiabox.admin_server import AdminServer


@pytest.fixture
def server(tmp_path):
    html_path = tmp_path / "index.html"
    html_path.write_text("<html><body>hello admin</body></html>", encoding="utf-8")
    poster_dir = tmp_path / "posters"
    poster_dir.mkdir()
    (poster_dir / "channel-02-arthur.jpg").write_bytes(b"\xff\xd8\xff\xd9")  # minimal jpeg-ish bytes

    state = {"mode": "grid", "sections": []}

    srv = AdminServer(
        html_path=html_path,
        poster_dir=poster_dir,
        state_provider=lambda: state,
        host="127.0.0.1",
        port=0,
    )
    srv.start()
    srv._test_state = state  # type: ignore[attr-defined]  - lets tests mutate it
    yield srv
    srv.stop()


def _get(url: str):
    with urllib.request.urlopen(url, timeout=5) as resp:
        return resp.status, resp.headers.get("Content-Type"), resp.read()


def test_serves_index_html(server):
    status, content_type, body = _get(server.url)
    assert status == 200
    assert "text/html" in content_type
    assert b"hello admin" in body


def test_serves_state_as_json(server):
    status, content_type, body = _get(server.url + "state")
    assert status == 200
    assert content_type == "application/json"
    assert json.loads(body) == {"mode": "grid", "sections": []}


def test_state_reflects_live_changes_not_a_snapshot_at_start(server):
    server._test_state["mode"] = "episode_list"  # type: ignore[attr-defined]
    _, _, body = _get(server.url + "state")
    assert json.loads(body)["mode"] == "episode_list"


def test_serves_a_poster_file(server):
    status, content_type, body = _get(server.url + "poster/channel-02-arthur.jpg")
    assert status == 200
    assert content_type == "image/jpeg"
    assert body == b"\xff\xd8\xff\xd9"


def test_missing_poster_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server.url + "poster/does-not-exist.jpg")
    assert exc.value.code == 404


def test_path_traversal_in_poster_name_is_rejected(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server.url + "poster/..%2F..%2Fetc%2Fpasswd.jpg")
    assert exc.value.code in (400, 404)


def test_unknown_route_is_404(server):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server.url + "nope")
    assert exc.value.code == 404


def test_start_is_idempotent(server):
    port_before = server.url
    server.start()  # already running - must not raise or rebind
    assert server.url == port_before


def test_state_endpoint_500s_if_provider_raises(tmp_path):
    html_path = tmp_path / "index.html"
    html_path.write_text("<html></html>", encoding="utf-8")

    def boom():
        raise RuntimeError("snapshot failed")

    srv = AdminServer(html_path=html_path, poster_dir=tmp_path, state_provider=boom, port=0)
    srv.start()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            _get(srv.url + "state")
        assert exc.value.code == 500
    finally:
        srv.stop()
