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


@dataclass(frozen=True)
class ChannelInsight:
    """One channel's/game system's rolled-up stats for the admin Insights
    view (UKE-29).

    ``watched_minutes`` is best-effort and shows-only: RetroArch gives no
    duration/position signal for a game (see the module docstring), so a
    game channel's minutes are always ``None`` rather than a misleading 0 -
    ``play_count`` is the honest signal for games instead.
    """

    number: int
    name: str
    kind: str  # "show" or "game", matches Channel.config.kind
    watched_count: int  # episodes watched (shows) / roms played at least once (games)
    total_count: int
    watched_minutes: Optional[int]
    play_count: int  # games only; always 0 for shows
    last_played: float  # unix timestamp, 0.0 if never touched


@dataclass(frozen=True)
class ActivityEntry:
    """One row of the Insights view's recent-activity log (UKE-29), most
    recent first - a simple "what got watched/played, and when" feed built
    from the same last_played timestamps everything else here already
    tracks, not a separate event log.
    """

    channel_number: int
    channel_name: str
    kind: str
    title: str
    when: float  # unix timestamp
    watched: bool  # shows: fully watched vs. still in-progress; games: always True


@dataclass(frozen=True)
class InsightsSummary:
    """Everything the admin Insights screen needs, computed fresh each time
    it's opened (see TVApp._insights_snapshot) - cheap enough (in-memory
    dict lookups over however many channels/episodes exist) that there's no
    need to cache or incrementally maintain it, same tradeoff as
    continue_watching().
    """

    channels: List[ChannelInsight]
    favorite: Optional[ChannelInsight]
    activity: List[ActivityEntry]
    total_watched_minutes: int
    total_episodes_watched: int
    total_games_played: int


def insights_summary(
    channels: Sequence[Channel], watch_state: Optional["WatchState"], *, activity_limit: int = 20
) -> InsightsSummary:
    """Roll up per-channel stats, an overall "favorite", and a recent-
    activity feed across every real channel and game system. ``channels`` is
    real channels + game systems combined (see TVApp._admin_tiles), same as
    continue_watching().
    """
    if watch_state is None:
        return InsightsSummary(
            channels=[], favorite=None, activity=[],
            total_watched_minutes=0, total_episodes_watched=0, total_games_played=0,
        )

    per_channel: List[ChannelInsight] = []
    activity: List[ActivityEntry] = []
    total_minutes = 0
    total_episodes_watched = 0
    total_games_played = 0

    for channel in channels:
        is_game = channel.config.kind == "game"
        total_count = len(channel.episodes)
        watched_count = 0
        minutes = 0
        play_count = 0
        last_played = 0.0

        for item_path in channel.episodes:
            if is_game:
                gstate = watch_state.game_state(channel.number, channel.config.path, item_path)
                if gstate.played:
                    watched_count += 1
                play_count += gstate.play_count
                if gstate.last_played:
                    last_played = max(last_played, gstate.last_played)
                    activity.append(
                        ActivityEntry(
                            channel_number=channel.number, channel_name=channel.name,
                            kind="game", title=episode_title(item_path),
                            when=gstate.last_played, watched=True,
                        )
                    )
            else:
                estate = watch_state.episode_state(channel.number, channel.config.path, item_path)
                if estate.watched:
                    watched_count += 1
                    if estate.duration:
                        minutes += round(estate.duration / 60)
                elif estate.in_progress:
                    minutes += round(estate.position / 60)
                if estate.last_played:
                    last_played = max(last_played, estate.last_played)
                    activity.append(
                        ActivityEntry(
                            channel_number=channel.number, channel_name=channel.name,
                            kind="show", title=episode_title(item_path),
                            when=estate.last_played, watched=estate.watched,
                        )
                    )

        per_channel.append(
            ChannelInsight(
                number=channel.number, name=channel.name, kind=channel.config.kind,
                watched_count=watched_count, total_count=total_count,
                watched_minutes=None if is_game else minutes,
                play_count=play_count, last_played=last_played,
            )
        )
        if is_game:
            total_games_played += play_count
        else:
            total_minutes += minutes
            total_episodes_watched += watched_count

    activity.sort(key=lambda a: a.when, reverse=True)
    # "Favorite" is whichever channel has the most engagement in its own
    # terms (watched minutes for shows, play count for games) - comparing
    # those two units directly wouldn't mean much, but picking the max of
    # "the number that matters for this channel's kind" across everything
    # touched at least once is a reasonable single answer.
    touched = [c for c in per_channel if c.last_played > 0]
    favorite = max(
        touched,
        key=lambda c: c.play_count if c.kind == "game" else (c.watched_minutes or 0),
        default=None,
    )

    return InsightsSummary(
        channels=per_channel,
        favorite=favorite,
        activity=activity[:activity_limit],
        total_watched_minutes=total_minutes,
        total_episodes_watched=total_episodes_watched,
        total_games_played=total_games_played,
    )


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
    "ChannelInsight",
    "ActivityEntry",
    "InsightsSummary",
    "insights_summary",
    "STATE_SUBDIR",
    "STATE_FILENAME",
    "WATCHED_THRESHOLD",
    "IN_PROGRESS_FLOOR",
]
