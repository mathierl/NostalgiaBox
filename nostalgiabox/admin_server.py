"""A tiny local HTTP server behind admin mode's real HTML/CSS UI.

Why this exists: mpv's ASS-overlay rendering (see overlay.py/thumbnails.py)
can only draw flat text and simple vector shapes over one pre-baked
background image - it was never going to reproduce a real Netflix-style
browsing screen (rounded cards, real poster art at real size, shadows,
smooth typography). Admin mode now hands the display to a kiosk-mode
Chromium instead (see TVApp._enter_admin_ui/_exit_admin_ui in app.py, and
scripts/spike_mpv_chromium_handoff.py for the DRM-handoff groundwork), which
loads a small local web app served by this module.

Deliberately one-directional: **all input still goes through
nostalgiabox.input.InputManager and TVApp**, exactly as before - the
controller/keyboard/CEC handling that already existed doesn't change at all.
The browser is a pure renderer with no interactivity of its own: its only
job is to poll ``GET /state`` a few times a second and redraw the DOM from
whatever JSON comes back, the same way a smart-mirror display works. This
means every bit of cursor/navigation logic already built and tested in
TVApp (sections, Continue Watching, episode lists, ...) is reused completely
unchanged - only the *rendering* moved, not the state machine.

Routes:
    GET /            the admin UI's index.html
    GET /state       JSON snapshot of current admin state (see
                      TVApp._admin_state_snapshot)
    GET /poster/<n>.jpg   a channel/game system's poster image, if one has
                      been generated (see thumbnails.py); 404 otherwise
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Dict, Optional

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


class AdminServer:
    """Runs in a background thread for the lifetime of the process (started
    lazily, the first time admin mode is entered - see TVApp). Cheap enough
    to just leave running rather than starting/stopping it around every
    admin-mode toggle, and that way a slow-starting Chromium never races a
    server that isn't listening yet.
    """

    def __init__(
        self,
        *,
        html_path: Path,
        poster_dir: Path,
        state_provider: Callable[[], Dict],
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
    ) -> None:
        self._html_path = html_path
        self._poster_dir = poster_dir
        self._state_provider = state_provider
        self._host = host
        self._port = port
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        # After start(), reflects the actual bound port - relevant when
        # constructed with port=0 (e.g. in tests), which asks the OS for
        # any free port rather than a fixed one.
        port = self._httpd.server_address[1] if self._httpd is not None else self._port
        return f"http://{self._host}:{port}/"

    def start(self) -> None:
        if self._httpd is not None:
            return  # already running
        handler = _make_handler(self._html_path, self._poster_dir, self._state_provider)
        self._httpd = ThreadingHTTPServer((self._host, self._port), handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="admin-server", daemon=True
        )
        self._thread.start()
        log.info("admin server listening on %s", self.url)

    def stop(self) -> None:
        if self._httpd is None:
            return
        self._httpd.shutdown()
        self._httpd.server_close()
        self._httpd = None
        self._thread = None


def _make_handler(html_path: Path, poster_dir: Path, state_provider: Callable[[], Dict]):
    class Handler(BaseHTTPRequestHandler):
        # Quiet by default - polling /state a few times a second would
        # otherwise spam the log at INFO on every request.
        def log_message(self, fmt: str, *args) -> None:  # noqa: A003
            log.debug("admin_server: " + fmt, *args)

        def do_GET(self) -> None:  # noqa: N802 - stdlib naming
            if self.path == "/" or self.path == "/index.html":
                self._serve_file(html_path, "text/html; charset=utf-8")
            elif self.path == "/state":
                self._serve_json()
            elif self.path.startswith("/poster/") and self.path.endswith(".jpg"):
                self._serve_poster()
            else:
                self.send_error(404)

        def _serve_file(self, path: Path, content_type: str) -> None:
            try:
                data = path.read_bytes()
            except OSError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _serve_json(self) -> None:
            try:
                payload = json.dumps(state_provider()).encode("utf-8")
            except Exception:  # noqa: BLE001
                log.exception("admin_server: failed to build state snapshot")
                self.send_error(500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _serve_poster(self) -> None:
            name = self.path[len("/poster/") :]
            # No path traversal: only a bare "<something>.jpg" filename, no
            # slashes, is ever accepted - reject anything that would climb
            # out of poster_dir before it ever touches the filesystem.
            if "/" in name or "\\" in name or not name.endswith(".jpg"):
                self.send_error(400)
                return
            self._serve_file(poster_dir / name, "image/jpeg")

    return Handler


__all__ = ["AdminServer", "DEFAULT_HOST", "DEFAULT_PORT"]
