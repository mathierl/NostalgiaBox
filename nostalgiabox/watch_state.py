"""Per-episode/per-game watch state: what's been watched, how far into it,
and what's been played - the persistence layer behind "continue watching"
and the admin insights view (see UKE-29).

Nothing in nostalgiabox persisted anything to disk before this - not even
the existing per-channel resume position (``Channel._resume_path`` /
``_resume_position``) survives a reboot; that's pure in-memory state used
only by the "resume" tune-in mode. This module is a small, dependency-free
JSON store, written atomically (temp file + ``os.replace``) so a mid-write
power-cycle - this is a Pi that gets unplugged, not gracefully shut down -
can't leave a corrupt file behind.

Shows and games are tracked differently on purpose, not just cosmetically:
RetroArch gives no duration/position signal for a game, only "it ran", so
a game's state is a simple play count, never a resume position. A real
"continue where you left off" for a game would mean RetroArch save states
- a materially bigger, per-core-reliability-sensitive feature - and is
deliberately out of scope here.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .channel import Channel, episode_title

log = logging.getLogger(__name__)

STATE_SUBDIR = "state"
STATE_FILENAME = "watch_state.json"

# Fraction of an episode's duration that counts as "watched" - matches the
# common streaming-service convention of not requiring the very last second
# (credits, black frames) to count.
WATCHED_THRESHOLD = 0.9
# Below this fraction, treat a saved position as "barely started" rather
# than something worth surfacing as "continue watching".
IN_PROGRESS_FLOOR = 0.02


@dataclass
class EpisodeState:
    position: float = 0.0
    duration: float = 0.0
    watched: bool = False
    last_played: float = 0.0  # unix timestamp

    @property
    def fraction(self) -> float:
        if self.duration <= 0:
            return 0.0
        return max(0.0, min(1.0, self.position / self.duration))

    @property
    def in_progress(self) -> bool:
        f = self.fraction
        return not self.watched and IN_PROGRESS_FLOOR < f < WATCHED_THRESHOLD


@dataclass
class GameState:
    played: bool = False
    play_count: int = 0
    last_played: float = 0.0


def _from_dict(cls, data: dict):
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


@dataclass(frozen=True)
class ContinueEntry:
    """One row of the admin "Continue Watching" list (UKE-29): a specific
    in-progress episode, ready to resume exactly where it was left off.
    """

    channel_number: int
    channel_name: str
    episode_path: Path
    title: str
    resume_position: float
    minutes_left: int
    last_played: float


def continue_watching(
    channels: Sequence[Channel], watch_state: Optional["WatchState"], *, limit: int = 10
) -> List[ContinueEntry]:
    """The most recently in-progress episodes across every real (non-game)
    channel, most recent first. Games are deliberately excluded - they have
    no resume position to continue from (see the module docstring).

    Cheap to call: everything here is dict lookups against the already-
    loaded in-memory watch state, no disk or subprocess work, so callers can
    recompute this freely (e.g. every time the admin browse grid is opened)
    rather than trying to keep it incrementally in sync.
    """
    if watch_state is None:
        return []
    entries: List[ContinueEntry] = []
    for channel in channels:
        if channel.config.kind == "game":
            continue
        for episode_path in channel.episodes:
            state = watch_state.episode_state(channel.number, channel.config.path, episode_path)
            if not state.in_progress:
                continue
            remaining = max(1, round((state.duration - state.position) / 60))
            entries.append(
                ContinueEntry(
                    channel_number=channel.number,
                    channel_name=channel.name,
                    episode_path=episode_path,
                    title=episode_title(episode_path),
                    resume_position=state.position,
                    minutes_left=remaining,
                    last_played=state.last_played,
                )
            )
    entries.sort(key=lambda e: e.last_played, reverse=True)
    return entries[:limit]


class WatchState:
    """In-memory watch state, loaded from and saved back to a JSON file.

    Keyed by ``"<channel number>:<path relative to that channel's folder>"``
    rather than an absolute path (survives the media folder moving, e.g. a
    different SD card or mount point) or a synthetic id (episodes aren't
    scanned with persistent identities anywhere else in this codebase).
    Renaming a source file loses its history - an accepted tradeoff, the
    same one most media servers without embedded metadata make.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._shows: Dict[str, EpisodeState] = {}
        self._games: Dict[str, GameState] = {}
        self._load()

    # -- keys -----------------------------------------------------------
    @staticmethod
    def _key(channel_number: int, root: Path, item_path: Path) -> str:
        try:
            rel = item_path.relative_to(root)
        except ValueError:
            rel = Path(item_path.name)
        return f"{channel_number}:{rel.as_posix()}"

    # -- shows ------------------------------------------------------------
    def episode_state(self, channel_number: int, root: Path, episode_path: Path) -> EpisodeState:
        return self._shows.get(self._key(channel_number, root, episode_path), EpisodeState())

    def record_episode_position(
        self,
        channel_number: int,
        root: Path,
        episode_path: Path,
        position: float,
        duration: float,
    ) -> None:
        key = self._key(channel_number, root, episode_path)
        state = self._shows.get(key) or EpisodeState()
        state.position = max(0.0, position)
        if duration:
            state.duration = duration
        state.last_played = time.time()
        if state.fraction >= WATCHED_THRESHOLD:
            state.watched = True
        self._shows[key] = state
        self._save()

    def mark_episode_watched(
        self, channel_number: int, root: Path, episode_path: Path, *, duration: Optional[float] = None
    ) -> None:
        """Mark an episode watched outright - used on a natural end-of-file,
        which is an unambiguous "watched" signal regardless of what the last
        sampled position happened to be.
        """
        key = self._key(channel_number, root, episode_path)
        state = self._shows.get(key) or EpisodeState()
        state.watched = True
        if duration:
            state.duration = duration
            state.position = duration
        state.last_played = time.time()
        self._shows[key] = state
        self._save()

    def all_episodes(self) -> Dict[str, EpisodeState]:
        return dict(self._shows)

    # -- games ------------------------------------------------------------
    def game_state(self, channel_number: int, root: Path, rom_path: Path) -> GameState:
        return self._games.get(self._key(channel_number, root, rom_path), GameState())

    def record_game_played(self, channel_number: int, root: Path, rom_path: Path) -> None:
        key = self._key(channel_number, root, rom_path)
        state = self._games.get(key) or GameState()
        state.played = True
        state.play_count += 1
        state.last_played = time.time()
        self._games[key] = state
        self._save()

    def all_games(self) -> Dict[str, GameState]:
        return dict(self._games)

    # -- persistence --------------------------------------------------------
    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("could not read watch state at %s; starting fresh", self._path)
            return
        for key, data in (raw.get("shows") or {}).items():
            self._shows[key] = _from_dict(EpisodeState, data)
        for key, data in (raw.get("games") or {}).items():
            self._games[key] = _from_dict(GameState, data)

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "shows": {k: asdict(v) for k, v in self._shows.items()},
                "games": {k: asdict(v) for k, v in self._games.items()},
            }
            fd, tmp_name = tempfile.mkstemp(dir=str(self._path.parent), prefix=".watch_state-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh)
                os.replace(tmp_name, self._path)
            except Exception:
                os.unlink(tmp_name)
                raise
        except OSError:
            log.exception("failed to save watch state to %s", self._path)


__all__ = [
    "WatchState",
    "EpisodeState",
    "GameState",
    "ContinueEntry",
    "continue_watching",
    "STATE_SUBDIR",
    "STATE_FILENAME",
    "WATCHED_THRESHOLD",
    "IN_PROGRESS_FLOOR",
]
