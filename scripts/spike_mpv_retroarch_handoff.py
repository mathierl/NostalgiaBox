#!/usr/bin/env python3
"""Spike: can mpv and RetroArch cleanly hand the DRM/KMS display back and
forth within the same process's lifetime?

This is the one open question behind the UKE-28 "games in admin mode" design
(see the Linear ticket): admin mode would stop mpv, launch RetroArch directly
for a picked game, then reopen mpv once RetroArch exits, all without
restarting the nostalgiabox process. Nothing in nostalgiabox today closes and
later recreates its MpvPlayer, so before building that feature we want to
know, on the real hardware:

  1. Does mpv.terminate() release the DRM master promptly enough for
     RetroArch to acquire the display right after, or is a delay needed?
  2. Does RetroArch's own DRM/KMS output actually get a picture in this
     handoff scenario?
  3. Can a *fresh* mpv.MPV(...) instance reopen cleanly afterward and show
     picture again?
  4. Is this reliable across repeated cycles, not just once?

This script is a standalone throwaway - it is NOT part of the nostalgiabox
package and doesn't import anything from it (deliberately, so it exercises
the raw mpv/RetroArch handoff in isolation, same options as
nostalgiabox.player.MpvPlayer but nothing else).

Run this ON THE PI, on the real display (not over SSH/X-forwarding), with
nostalgiabox.service stopped first so nothing else is holding the display:

    sudo systemctl stop nostalgiabox
    python3 scripts/spike_mpv_retroarch_handoff.py \\
        --core ~/.config/retroarch/cores/snes9x_libretro.so \\
        --rom /path/to/game.sfc \\
        --media /path/to/any/video/or/image/mpv/can/loop \\
        --audio-device plughw:CARD=vc4hdmi0,DEV=0

Watch the TV, not just the terminal - this script can only report what mpv
and RetroArch's exit codes say, not whether picture/audio actually appeared.
It pauses at the key moments and asks you to confirm what you're seeing.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def open_mpv(media_path: str, *, audio_device: str | None, gpu_context: str):
    import mpv  # type: ignore

    options = dict(
        osc=False,
        input_default_bindings=False,
        input_vo_keyboard=False,
        idle="yes",
        force_window="yes",
        keep_open="yes",
        fullscreen=True,
        hwdec="auto-safe",
        vo="gpu",
        cursor_autohide="always",
    )
    if audio_device:
        options["audio_device"] = audio_device
    if gpu_context:
        options["gpu_context"] = gpu_context

    player = mpv.MPV(**options)
    player.loop_file = "inf"
    player.loadfile(str(media_path), "replace")
    player.pause = False
    return player


def close_mpv(player) -> None:
    t0 = time.monotonic()
    try:
        player.terminate()
    except Exception as exc:  # noqa: BLE001
        log(f"   ! mpv.terminate() raised: {exc!r}")
    dt = time.monotonic() - t0
    log(f"   mpv.terminate() returned after {dt:.2f}s")


def run_retroarch(core: str, rom: str) -> int:
    cmd = ["retroarch", "-L", core, rom]
    log(f"   launching: {' '.join(cmd)}")
    t0 = time.monotonic()
    result = subprocess.run(cmd)
    dt = time.monotonic() - t0
    log(f"   retroarch exited (code {result.returncode}) after {dt:.1f}s")
    return result.returncode


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--core", required=True, help="path to a libretro core .so")
    parser.add_argument("--rom", required=True, help="ROM/disc image for that core")
    parser.add_argument(
        "--media", required=True,
        help="any video/image file mpv can loop, to prove the picture is really there",
    )
    parser.add_argument("--audio-device", default=None, help="e.g. plughw:CARD=vc4hdmi0,DEV=0")
    parser.add_argument("--gpu-context", default="drm")
    parser.add_argument(
        "--rounds", type=int, default=2,
        help="close/reopen cycles to run - repeatability matters more than one success (default: 2)",
    )
    parser.add_argument(
        "--settle", type=float, default=0.0,
        help="seconds to sleep after mpv.terminate() before launching RetroArch, "
        "to test whether a delay is what makes this reliable",
    )
    args = parser.parse_args()

    log("=== spike: mpv <-> RetroArch DRM handoff ===")
    log(f"rounds={args.rounds} settle={args.settle}s gpu_context={args.gpu_context}")

    for round_num in range(1, args.rounds + 1):
        log(f"--- round {round_num}/{args.rounds} ---")

        log("1. opening mpv, loading test media")
        player = open_mpv(args.media, audio_device=args.audio_device, gpu_context=args.gpu_context)
        input("   confirm: is picture showing on the TV? press Enter to continue... ")

        log("2. closing mpv")
        close_mpv(player)
        del player
        if args.settle:
            log(f"   sleeping {args.settle}s before launching RetroArch")
            time.sleep(args.settle)

        log("3. launching RetroArch - quit it normally (its own menu/hotkey) when done poking at it")
        rc = run_retroarch(args.core, args.rom)
        if rc != 0:
            log(f"   ! non-zero exit ({rc}) - note whether picture/audio appeared at all before this")

        log("4. reopening mpv")
        try:
            player2 = open_mpv(args.media, audio_device=args.audio_device, gpu_context=args.gpu_context)
        except Exception as exc:  # noqa: BLE001
            log(f"   ! mpv reopen FAILED: {exc!r}")
            log("   that's the answer we needed - stop here and report this, including --settle used")
            sys.exit(1)
        input("   confirm: did picture come back, or is it blank/black? press Enter to continue... ")
        close_mpv(player2)
        del player2

    log("=== done - report what you saw at each numbered step above, and whether it held across all rounds ===")


if __name__ == "__main__":
    main()
