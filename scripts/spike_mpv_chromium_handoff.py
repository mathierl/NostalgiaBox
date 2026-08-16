#!/usr/bin/env python3
"""Spike: can mpv and a kiosk-mode Chromium cleanly hand the display back and
forth, on a Pi that has no desktop environment at all?

This is the one real open question behind moving admin mode off mpv's ASS-
overlay rendering and onto a real HTML/CSS/React UI (see the Linear ticket):
admin mode would stop mpv, launch Chromium full-screen against a small local
web app, then reopen mpv once Chromium closes - the same shape of handoff
already proven for RetroArch (see spike_mpv_retroarch_handoff.py), but with a
much heavier, much less embedded-friendly program on the other end.

The real unknown, and the reason this is a spike and not just "build it": per
scripts/install.sh, this Pi has no X11 or Wayland session running - mpv talks
to the display directly via DRM/KMS. A normal desktop build of Chromium
expects a window server to hand it a window; there usually isn't one here.
Two ways around that, both tried below:

  1. --method ozone-drm: Chromium's own native DRM/KMS backend (the same
     approach ChromeOS kiosk devices use) - no extra package needed beyond
     Chromium itself, but support/flags vary a lot by Chromium build/version,
     so this may just fail to show anything.
  2. --method cage: run Chromium inside `cage`, a minimal Wayland compositor
     built for exactly this "one fullscreen kiosk app, nothing else" use case
     (common on Pi digital-signage setups). Needs `sudo apt install cage` -
     more moving parts, but a much more standard, better-supported path.

Run this ON THE PI, on the real display (not over SSH/X-forwarding), with
nostalgiabox.service stopped first so nothing else is holding the display:

    sudo systemctl stop nostalgiabox
    python3 scripts/spike_mpv_chromium_handoff.py \\
        --media ~/media/<some-show>/<some-episode>.mp4 \\
        --audio-device plughw:CARD=vc4hdmi0,DEV=0 \\
        --method ozone-drm

If that shows nothing (blank/black screen, or Chromium exits immediately),
install cage and try the other method:

    sudo apt install -y cage
    python3 scripts/spike_mpv_chromium_handoff.py --media ... --method cage

This script is a standalone throwaway - it doesn't import anything from
nostalgiabox, and it points Chromium at a generated local HTML file, not a
real running admin_server, to keep this test isolated to just the display
handoff question.

Watch the TV, not just the terminal - this script can only report what mpv
and Chromium's exit codes say, not whether picture actually appeared. It
pauses at the key moments and asks you to confirm what you're seeing.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_TEST_PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>spike</title>
<style>
  html, body { margin: 0; height: 100%; background: #0a2540; overflow: hidden; }
  .box {
    height: 100%; display: flex; flex-direction: column; align-items: center;
    justify-content: center; color: #fff; font: bold 72px sans-serif;
    text-align: center;
  }
  .sub { font-size: 32px; font-weight: normal; margin-top: 24px; color: #7fd4ff; }
  .pulse { animation: pulse 1.5s ease-in-out infinite; }
  @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
</style></head>
<body><div class="box">
  <div class="pulse">CHROMIUM KIOSK SPIKE</div>
  <div class="sub">if you can read this, the DRM handoff worked</div>
</div></body></html>
"""


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def open_mpv(media_path: str, *, audio_device: str | None, gpu_context: str, mute: bool):
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
    if mute:
        options["mute"] = "yes"

    player = mpv.MPV(**options)
    player.loop_file = "inf"
    player.loadfile(str(media_path), "replace")
    player.pause = False
    return player


def close_mpv(player) -> None:
    log("   calling mpv.terminate() - do NOT Ctrl+C here, just wait a couple seconds")
    t0 = time.monotonic()
    try:
        player.terminate()
    except Exception as exc:  # noqa: BLE001
        log(f"   ! mpv.terminate() raised: {exc!r}")
    dt = time.monotonic() - t0
    log(f"   mpv.terminate() returned after {dt:.2f}s")


def find_chromium() -> str:
    for name in ("chromium-browser", "chromium"):
        path = shutil.which(name)
        if path:
            return path
    log("! no chromium-browser/chromium binary found on PATH")
    log("  install it first: sudo apt install -y chromium-browser   (or: chromium)")
    sys.exit(2)


def run_chromium(page_path: Path, *, method: str, seconds: float) -> int:
    chromium = find_chromium()
    common_flags = [
        "--kiosk",
        "--noerrdialogs",
        "--disable-infobars",
        "--disable-session-crashed-bubble",
        "--check-for-update-interval=31536000",
        "--incognito",
        f"file://{page_path}",
    ]

    if method == "ozone-drm":
        cmd = [
            chromium,
            "--ozone-platform=drm",
            "--enable-features=UseOzonePlatform",
            *common_flags,
        ]
    elif method == "cage":
        cage = shutil.which("cage")
        if cage is None:
            log("! `cage` not found - install it: sudo apt install -y cage")
            sys.exit(2)
        cmd = [cage, "--", chromium, "--ozone-platform=wayland", *common_flags]
    else:
        raise ValueError(f"unknown method: {method}")

    log(f"   launching ({method}): {' '.join(cmd)}")
    t0 = time.monotonic()
    proc = subprocess.Popen(cmd)
    input(f"   confirm: is the test page showing on the TV? (waiting up to {seconds:.0f}s either way)"
          f" press Enter once you've looked... ")
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log("   chromium didn't exit on terminate() - killing it")
        proc.kill()
        proc.wait()
    dt = time.monotonic() - t0
    log(f"   chromium closed after {dt:.1f}s (exit code {proc.returncode})")
    return proc.returncode or 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--media", required=True, help="any video/image file mpv can loop")
    parser.add_argument("--audio-device", default=None, help="e.g. plughw:CARD=vc4hdmi0,DEV=0")
    parser.add_argument("--gpu-context", default="drm")
    parser.add_argument(
        "--method", choices=["ozone-drm", "cage"], default="ozone-drm",
        help="how to give Chromium a display with no desktop session running (default: ozone-drm, "
        "no extra package needed - try this first, fall back to cage if it shows nothing)",
    )
    parser.add_argument(
        "--rounds", type=int, default=2,
        help="close/reopen cycles to run - repeatability matters more than one success (default: 2)",
    )
    parser.add_argument("--seconds", type=float, default=6.0, help="how long to leave chromium up each round")
    parser.add_argument(
        "--mute", action="store_true", help="mute mpv for this run (chromium's test page has no audio anyway)"
    )
    args = parser.parse_args()

    tmpdir = Path(tempfile.mkdtemp(prefix="spike-chromium-"))
    page_path = tmpdir / "test.html"
    page_path.write_text(_TEST_PAGE, encoding="utf-8")

    log("=== spike: mpv <-> Chromium kiosk display handoff ===")
    log(f"method={args.method} rounds={args.rounds}")
    log(f"test page: {page_path}")

    for round_num in range(1, args.rounds + 1):
        log(f"--- round {round_num}/{args.rounds} ---")

        log("1. opening mpv, loading test media")
        player = open_mpv(
            args.media, audio_device=args.audio_device, gpu_context=args.gpu_context, mute=args.mute
        )
        input("   confirm: is picture showing on the TV? press Enter to continue... ")

        log("2. closing mpv")
        close_mpv(player)
        del player

        log("3. launching Chromium kiosk")
        rc = run_chromium(page_path, method=args.method, seconds=args.seconds)
        if rc != 0:
            log(f"   ! non-zero exit ({rc}) - note whether the test page appeared at all before this")

        log("4. reopening mpv")
        try:
            player2 = open_mpv(
                args.media, audio_device=args.audio_device, gpu_context=args.gpu_context, mute=args.mute
            )
        except Exception as exc:  # noqa: BLE001
            log(f"   ! mpv reopen FAILED: {exc!r}")
            log("   that's the answer we needed - stop here and report this, including --method used")
            sys.exit(1)
        input("   confirm: did picture come back, or is it blank/black? press Enter to continue... ")
        close_mpv(player2)
        del player2

    log("=== done - report what you saw at each numbered step above (which method, did the page")
    log("    actually render, did mpv come back cleanly), and whether it held across all rounds ===")


if __name__ == "__main__":
    main()
