"""Keyboard / USB / IR remote input via Linux evdev.

Most cheap "media remotes" (and IR remotes bridged through a USB receiver or
LIRC's uinput) show up to Linux as ordinary keyboard-like input devices. This
backend reads their key-down events straight from ``/dev/input/event*`` using
the ``evdev`` package - no X server or desktop required, which is exactly what
we want on a headless Pi wired to a TV.
"""

from __future__ import annotations

import logging
import select
import time
from typing import Dict, List, Optional, Sequence

from ..actions import Action, InputEvent
from .base import InputBackend
from .keymap import evdev_key_to_event

log = logging.getLogger(__name__)

# Key-event values reported by evdev: 0=up, 1=down, 2=autorepeat.
_KEY_UP = 0
_KEY_DOWN = 1
_KEY_REPEAT = 2


class KeyboardBackend(InputBackend):
    """Reads remote/keyboard events from evdev input devices."""

    name = "keyboard"

    def __init__(
        self,
        *,
        device_paths: Optional[Sequence[str]] = None,
        name_filter: Optional[str] = None,
        grab: bool = False,
        allow_repeat: bool = True,
        overrides: Optional[Dict[str, Optional[InputEvent]]] = None,
        admin_hold_seconds: Optional[float] = None,
    ) -> None:
        super().__init__()
        self._device_paths = list(device_paths) if device_paths else None
        self._name_filter = name_filter.lower() if name_filter else None
        self._grab = grab
        self._allow_repeat = allow_repeat
        # Per-key action overrides from config (key name -> InputEvent or None).
        self._overrides = dict(overrides or {})
        self._devices: List = []
        # Long-press detection for the secret admin/developer view: how long
        # (seconds) a key mapped to Action.POWER must be held for release to
        # fire ADMIN_TOGGLE instead of the normal power toggle. None disables
        # the feature entirely (a short press always toggles standby, as before).
        self._admin_hold_seconds = admin_hold_seconds
        self._power_down_key: Optional[str] = None
        self._power_down_at: Optional[float] = None

    def _lookup(self, key_name: str) -> Optional[InputEvent]:
        """Config overrides win over the built-in defaults."""
        if key_name in self._overrides:
            return self._overrides[key_name]  # may be None (explicitly unbound)
        return evdev_key_to_event(key_name)

    @staticmethod
    def is_available() -> bool:
        try:
            import evdev  # noqa: F401
        except ImportError:
            return False
        return True

    def _open_devices(self):
        import evdev
        from evdev import ecodes

        paths = self._device_paths or evdev.list_devices()
        devices = []
        for path in paths:
            try:
                dev = evdev.InputDevice(path)
            except (OSError, PermissionError) as exc:
                log.warning("cannot open input device %s: %s", path, exc)
                continue
            caps = dev.capabilities()
            if ecodes.EV_KEY not in caps:
                dev.close()
                continue
            if self._name_filter and self._name_filter not in (dev.name or "").lower():
                dev.close()
                continue
            if self._grab:
                try:
                    dev.grab()
                except OSError:
                    log.warning("could not grab %s (continuing ungrabbed)", dev.name)
            log.info("listening to input device: %s (%s)", dev.name, path)
            devices.append(dev)
        return devices

    def _run(self) -> None:
        if not self.is_available():
            log.error("evdev is not installed; keyboard/remote input disabled")
            return
        self._devices = self._open_devices()
        if not self._devices:
            log.warning("no usable input devices found for the keyboard backend")
            return

        from evdev import ecodes

        fd_to_device = {dev.fd: dev for dev in self._devices}
        while not self.stopping:
            try:
                r, _, _ = select.select(fd_to_device, [], [], 0.5)
            except (OSError, ValueError):
                break
            for fd in r:
                dev = fd_to_device.get(fd)
                if dev is None:
                    continue
                try:
                    for event in dev.read():
                        if event.type != ecodes.EV_KEY:
                            continue
                        self._handle_key_event(event)
                except OSError:
                    log.warning("input device %s disappeared", getattr(dev, "path", "?"))
                    fd_to_device.pop(fd, None)

    def _handle_key_event(self, event) -> None:
        from evdev import ecodes

        key_name = _code_to_name(ecodes.KEY, event.code)
        if key_name is None:
            return
        input_event = self._lookup(key_name)
        if input_event is None:
            return

        # Long-press handling for the power button: rather than acting the
        # instant it's pressed, wait for release and look at how long it was
        # held. A normal press still toggles standby exactly as before; a
        # hold past the configured threshold instead fires the secret
        # admin/developer-view trigger. Timing is done off real press/release
        # events rather than autorepeat, so it works even on remotes whose
        # driver doesn't emit repeats for this key.
        if input_event.action is Action.POWER and self._admin_hold_seconds is not None:
            if event.value == _KEY_DOWN:
                self._power_down_key = key_name
                self._power_down_at = time.monotonic()
                return
            if event.value == _KEY_UP:
                if key_name == self._power_down_key and self._power_down_at is not None:
                    held = time.monotonic() - self._power_down_at
                    self._power_down_key = None
                    self._power_down_at = None
                    if held >= self._admin_hold_seconds:
                        self.emit(InputEvent(Action.ADMIN_TOGGLE))
                    else:
                        self.emit(input_event)
                return
            return  # ignore autorepeat while timing a power hold

        if event.value == _KEY_DOWN:
            pass
        elif event.value == _KEY_REPEAT and self._allow_repeat:
            pass
        else:
            return  # key-up, or repeats when disabled

        # Only volume/channel keys should auto-repeat when held; ignore repeats
        # for digits, enter, power, etc. so a held button doesn't misbehave.
        if event.value == _KEY_REPEAT and input_event.action not in (
            Action.VOLUME_UP,
            Action.VOLUME_DOWN,
            Action.CHANNEL_UP,
            Action.CHANNEL_DOWN,
        ):
            return
        self.emit(input_event)

    def _close(self) -> None:
        for dev in self._devices:
            try:
                if self._grab:
                    dev.ungrab()
            except OSError:
                pass
            try:
                dev.close()
            except OSError:
                pass
        self._devices = []


def _code_to_name(key_table, code: int) -> Optional[str]:
    """evdev's KEY table maps a code to a name or a list of aliases."""
    name = key_table.get(code)
    if name is None:
        return None
    if isinstance(name, (list, tuple)):
        return name[0] if name else None
    return name


__all__ = ["KeyboardBackend"]
