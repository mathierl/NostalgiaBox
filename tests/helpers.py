"""Shared test helpers."""

from __future__ import annotations

from pathlib import Path
from typing import List


def make_show(root: Path, name: str, episodes: int, ext: str = ".mp4") -> Path:
    """Create a show folder with ``episodes`` dummy episode files."""
    folder = root / name
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(1, episodes + 1):
        (folder / f"{name}_ep{i:02d}{ext}").write_bytes(b"\x00")
    return folder


class FakeClock:
    """A manually-advanced monotonic clock for deterministic timing tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def list_names(paths: List[Path]) -> List[str]:
    return [p.name for p in paths]


class FakeAdminUiProcess:
    """A fake subprocess.Popen-alike for testing TVApp's admin-UI launch/
    close handoff (see app.py's _open_admin_ui/_close_admin_ui) without
    actually spawning a browser.
    """

    def __init__(self) -> None:
        self.terminated = False
        self.killed = False

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout=None) -> int:
        return 0


def make_admin_ui_launcher():
    """Returns (launcher, calls, processes): a fake admin_ui_launcher that
    records each URL it was launched with and hands back a fresh
    FakeAdminUiProcess each time, so tests can assert both how many times
    the browser was (re)launched and that each one was properly closed.
    """
    calls = []
    processes = []

    def launcher(url):
        calls.append(url)
        proc = FakeAdminUiProcess()
        processes.append(proc)
        return proc

    return launcher, calls, processes
