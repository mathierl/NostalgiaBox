# NostalgiaBox

**Turn a Raspberry Pi into a retro TV for your kids.**

NostalgiaBox plays folders of old children's shows off an SD card as if they were
real TV **channels**. Flip to a channel and a show is already playing (starting a
few seconds in, like you just tuned in); when an episode ends, the next one rolls
automatically on an endless shuffle. It boots straight to the TV on power-up, is
driven by a simple remote, sends audio over HDMI, and has an authentic
early-2000s vibe — a green on-screen channel banner and volume bar, and a curved
"CRT" picture. No menus, no apps, no touchscreens. Just a remote and channels. Grown-ups get
a hidden, full-width admin view (hold Power) with a modern, Netflix-style
channel and episode browser, poster art, watch progress, and a sticky
"Adult Mode" that unlocks pause/seek/subtitles while watching - kids never
see any of it.

This guide has two parts:

1. [**The hardware you'll need**](#1-hardware)
2. [**Step-by-step setup**](#2-step-by-step-setup) — the SD card, the terminal, and the programming

---

## 1. Hardware

Everything you need to build one:

| Part | Link | What it's for |
|------|------|---------------|
| **Raspberry Pi 4 Model B** | https://amzn.to/4w6HcSC | The "brain" of the box (2GB RAM or more is plenty) |
| **Flirc USB Remote Adapter** | https://amzn.to/4h7hZ5O | Plugs into the Pi and lets **any** remote control it |
| **Simple TV Remote** | https://amzn.to/4wId7bZ | The big-button remote your kids will actually use |
| **Micro-HDMI → Full HDMI cable** | https://amzn.to/4pn1TXS | Connects the Pi to the TV (the Pi 4 uses micro-HDMI) |
| **Raspberry Pi 4 case** | https://amzn.to/4fg4RJ5 | Housing so it looks tidy next to the TV |

**You'll also need (you may already have these):**

- A **micro SD card**, 32 GB or larger. Bigger = more shows. (This holds the
  operating system *and* your video files.)
- A **USB-C power supply** for the Pi 4 (the official 3A one is recommended).
- A **TV with an HDMI port**.
- A **computer** (Mac or Windows) to set up the SD card and program the remote.
- Your **show video files** (e.g. `.mp4`/`.mkv` episodes you own).

---

## 2. Step-by-step setup

Take it one part at a time. You do the first two parts on your **computer**, then
the rest by connecting to the Pi.

### Part A — Prepare the SD card

1. On your computer, install the **Raspberry Pi Imager** from
   [raspberrypi.com/software](https://www.raspberrypi.com/software/).
2. Put the micro SD card into your computer.
3. Open Raspberry Pi Imager and choose:
   - **Device:** Raspberry Pi 4
   - **Operating System:** *Raspberry Pi OS Lite (64-bit)* (under "Raspberry Pi
     OS (other)"). "Lite" has no desktop — perfect, since the box boots straight
     to the TV.
   - **Storage:** your SD card
4. Click **Next → Edit Settings** (the gear/⚙ customization step) and set:
   - **Hostname:** `nostalgiabox`
   - **Enable SSH** → "Use password authentication"
   - **Username & password** (remember these!)
   - **Wi-Fi** name and password (needed once, for the initial download)
5. Write it, then eject the card.

### Part B — Assemble and power on

1. Put the Pi in its case.
2. Plug the **Flirc** adapter into a USB port on the Pi.
3. Connect the **micro-HDMI → HDMI** cable from the Pi to your TV.
4. Insert the SD card.
5. Plug in power. Wait ~1 minute for it to boot.

### Part C — Open the terminal and connect to the Pi

You'll control the Pi from your computer over the network (SSH).

- **Mac:** open the **Terminal** app.
- **Windows:** open **PowerShell**.

Then connect (use the username you set; hostname is `nostalgiabox`):

```bash
ssh pi@nostalgiabox.local
```

- The first time, type `yes` to accept.
- Enter your password (the screen stays blank while you type — that's normal).

You're "inside" the Pi when the prompt changes to something like
`pi@nostalgiabox:~ $`.

> If `nostalgiabox.local` doesn't resolve, find the Pi's IP address from your
> router and use `ssh pi@THAT.IP.ADDRESS` instead.

### Part D — Install NostalgiaBox

Install git (if needed), download the project, and run the installer:

```bash
sudo apt update
sudo apt install -y git
git clone https://github.com/landonbtw/NostalgiaBox.git
cd NostalgiaBox
./scripts/install.sh
```

The installer sets up everything: the media player (mpv), video tools (ffmpeg),
the retro font, and all dependencies. It takes a few minutes. Say `y` if it asks
to continue. It's done when you see **"==> Done!"**.

### Part E — Load your shows

Put each show in its **own folder**, one folder per channel. For example, on a
USB drive or copied onto the Pi:

```
/media/nostalgiabox/
├── Dragon Tales/
│   ├── S01E01.mp4
│   └── S01E02.mp4
├── Arthur/
└── The Magic School Bus/
```

The easiest way to get files onto the Pi is a **USB drive**: create the show
folders on it from your computer, copy your episodes in, plug it into the Pi, and
copy them over (ask for the exact copy commands if you need them). Any common
video format works (`.mp4`, `.mkv`, `.avi`, `.m4v`, …), and season sub-folders
are fine.

### Part F — Set up your channels

The installer already created a `config.yaml` for you from the template. Open it
and point the channels at your show folders:

```bash
nano config.yaml
```

A minimal example (see [`config.example.yaml`](config.example.yaml) for every
option):

```yaml
channels:
  - number: 2
    name: "Dragon Tales"
    path: /media/nostalgiabox/dragon-tales
  - number: 3
    name: "Arthur"
    path: /media/nostalgiabox/arthur

tune_in: random          # a random episode starts when you flip to a channel
start_offset: [6, 10]    # begin each show 6-10 seconds in (skips the intro)
```

Save in nano with **Ctrl+O**, Enter, then exit with **Ctrl+X**. Check it:

```bash
nostalgiabox --check
```

This lists your channels and how many episodes it found in each. (You can also
leave out specific seasons/specials per channel — see `exclude_seasons` and
`exclude` in the example config.)

### Part G — Program the remote (Flirc)

The **Flirc** adapter learns your remote and turns its buttons into keys
NostalgiaBox understands. Do this **on your computer**:

1. Unplug the Flirc from the Pi and plug it into your computer.
2. Install the **Flirc** app from [flirc.tv/downloads](https://flirc.tv/pages/downloads).
3. In the app, choose the **Full Keyboard** controller.
4. Click a key on the on-screen keyboard, then press the button on your remote
   you want to use for it. Every action has exactly one meaning, everywhere -
   nothing is contextually repurposed - so once trained, these buttons just
   work. Map these:

   | Click this on-screen key | Press this remote button | Does |
   |--------------------------|--------------------------|------|
   | **Up arrow (↑)**   | Channel-Up button   | Channel up |
   | **Down arrow (↓)** | Channel-Down button | Channel down |
   | **Right arrow (→)**| Volume-Up button    | Volume up |
   | **Left arrow (←)** | Volume-Down button  | Volume down |
   | **m**              | Mute button         | Mute |
   | **p**              | Power button        | Standby (blank the screen) |
   | **Enter**          | D-pad center/OK button | Confirm selection; pause/play once watching under Adult Mode |
   | **f**              | D-pad right button  | Seek forward (grid nav while browsing; skip ahead under Adult Mode) |
   | **r**              | D-pad left button   | Seek backward (grid nav while browsing; skip back under Adult Mode) |
   | **l**              | Back button         | Last channel (jumps to current show's episode list under Adult Mode) |
   | **i**              | Home button         | Info / subtitle toggle under Adult Mode |

   Leave the D-pad's Up/Down buttons untrained for now - they don't map to
   anything yet.

5. Unplug the Flirc from your computer and plug it back into the Pi.

That's it — no config changes needed; these keys work out of the box. (Advanced:
you can remap any key via `key_overrides` in the config — see the example.)

### Part H — Get audio out the TV (HDMI)

The Pi sometimes sends audio to its headphone jack by default. To force it out
HDMI, find your HDMI audio device:

```bash
nostalgiabox --list-audio
```

Look for the **HDMI** entry (e.g. `alsa/hdmi:CARD=vc4hdmi0,DEV=0`). The Pi 4 has
two HDMI ports: the one nearest the USB-C power is `vc4hdmi0`, the other is
`vc4hdmi1`. Put the matching name in `config.yaml`:

```yaml
audio_device: "alsa/hdmi:CARD=vc4hdmi0,DEV=0"   # use vc4hdmi1 if on the 2nd port
```

### Part I — Make it boot to TV on power-up

Test it first:

```bash
nostalgiabox
```

Your shows should appear on the TV and respond to the remote. Press `q` on a
keyboard (or `Ctrl+C` in SSH) to stop. Happy with it? Turn on auto-start:

```bash
./scripts/install.sh --service
```

Now the box boots straight to TV whenever it gets power — no login, no menus.

### Part J — Make it kid-proof (recommended)

Kids will unplug it. Two things keep the SD card from getting corrupted:

- **Turn it off with the remote:** turn the volume all the way down to 0, then
  press volume-down **once more** — the Pi shuts down cleanly ("GOODBYE"), and
  it's safe to unplug once the green light stops blinking.
- **Read-only mode (belt-and-suspenders):** run `sudo raspi-config` →
  **Performance Options → Overlay File System → Enable** (and write-protect the
  boot partition). This makes the SD read-only, so pulling the plug can *never*
  corrupt it. (To update later, disable the overlay, update, then re-enable it.)

**Done!** Plug it in and enjoy your nostalgia box.

---

## Using it day to day

| Do this | On the remote |
|---------|---------------|
| Change channels | Channel up / down |
| Adjust volume | Volume up / down |
| Mute | Mute |
| Seek / navigate (see Adult Mode) | D-pad left / right |
| Pause / play (see Adult Mode) | D-pad OK |
| Standby (blank screen) | Power |
| **Turn off** (safe to unplug) | Volume-down again when already at 0 |
| **Admin/developer view** (grown-ups only) | Hold Power for ~3 seconds |

Turn it on by plugging in power; it boots back to a channel automatically.

### Admin/developer view

Holding **Power** for about 3 seconds (instead of a quick press, which just
toggles standby) opens a hidden screen meant for adults: a modern, dark,
**full-width** "Select a channel" grid with a real poster thumbnail for every
channel (auto-generated from each show's first episode), its episode count,
and its watch progress, with the current one ringed. If there's more here
than fits on one screen, it wraps onto extra rows and scrolls - posters stay
a fixed, comfortable size rather than shrinking to cram everything into one
row. It's a two-step browser:

1. **Show grid.** **Channel Up/Down** moves the selection up/down a row,
   **Volume Up/Down** or the **D-pad left/right** (either works) moves it
   left/right within a row, scrolling as needed to keep the highlight on
   screen (this is just navigation - nothing changes yet). Below the real
   channels sit two rows that are always there
   regardless of what's configured: **Watch Insights** (see below) and an
   **Adult Mode** toggle. **Mute** confirms whatever's highlighted - a show
   opens its episode list, Insights opens the stats screen, and the Adult
   Mode row flips it on/off with a brief on-screen confirmation. Once Adult
   Mode is on, a third row appears below it: **Open RetroArch** (see
   "Games (arcade)" below).
2. **Episode list.** A numbered list of every episode in that show, each one
   showing its watch state - "✓ Watched" or "N% watched" for anything
   in progress. **Channel Up/Down** moves the highlighted episode. **Mute**
   confirms it and starts playing that exact episode. **Power** (a normal
   press, not a hold) backs out to the show grid instead of going to standby.

**Hold Power** again at any point - grid, episode list, or once something's
playing - to close the admin view. If you close it without ever confirming an
episode, playback resumes exactly where it was when you opened it; nothing is
interrupted just by looking around. Closing the browse screens on their own
never leaves anything lingering on screen - no permanent overlay, just the
picture.

#### Adult Mode

Turning on the **Adult Mode** row in the grid unlocks a grown-up-only control
surface while a show is actually playing, and - unlike everything else in
this section - it's *sticky*: it stays on across closing the grid and
changing channels, until you flip it off again from that same row (or it gets
reset by standby, see below).

While Adult Mode is on and you're just watching (not browsing):

- **D-pad OK/Enter** becomes **pause/play** - a control the kid remote never
  exposes, since a small kid pausing the TV mid-show tends to end in tears.
- **D-pad left/right** (the dedicated seek control) skips forward/backward
  within the current episode by `admin_seek_seconds` (default `10.0`),
  shown as a "»"/"«" OSD message with the resulting position.
- **Info** toggles subtitles on/off instead of flashing the channel banner.
- **Back/Last-channel** jumps straight into the current show's episode list
  - a one-button shortcut for switching to a different episode without first
  reopening the whole grid.

**Channel Up/Down, Volume Up/Down, and Mute always keep their normal literal
meaning, in every mode** - Adult Mode never repurposes them, so a kid (or
anyone else) picking up the remote can always change the channel, adjust
volume, or mute, exactly as expected. Only the D-pad's OK button and its
left/right seek control change behavior, and only while actually watching
under Adult Mode.

Every one of these shows a brief on-screen message and nothing more - the
same transient style as the volume bar - so there's never a persistent panel
glued to the picture.

A couple of notes:

- Poster thumbnails are generated automatically (one frame grabbed from each
  channel's first episode via `ffmpeg`, cached to disk) whenever you run
  `nostalgiabox --check` or reinstall/update - nothing to do by hand. If a
  channel has no readable video yet, its tile just falls back to a plain
  placeholder instead of a poster.
- It only works while a channel is on screen - not from standby, and putting
  the box into standby closes the browse screens *and* turns Adult Mode off,
  un-pausing first, so a kid power-cycling the box can never get stuck on a
  paused, half-browsed, or unlocked screen.
- Turn it off entirely with `admin_mode_enabled: false` in `config.yaml`, or
  change how long the hold needs to be with `admin_hold_seconds` (default
  `3.0`).
- Change the seek skip size with `admin_seek_seconds` (default `10.0`,
  1-300).
- Set whether subtitles start on or off with `subtitles_default` (default
  `false`).
- In `--dry-run` dev mode on a laptop keyboard, press **a** instead of timing
  a hold.

#### Games (arcade)

Once Adult Mode is on (see above), the grid gets a third row: **Open
RetroArch**. Confirming it hands the whole screen straight to RetroArch's
own menu - no channel/ROM picker of NostalgiaBox's own, just plain
`retroarch` with no arguments, exactly like launching it on any other
RetroArch setup. From there it's all RetroArch: its own playlists, cores,
save states, cheats, and settings. Quit back out through RetroArch's own
menu (F1 by default) and you land right back on the NostalgiaBox grid,
Adult Mode still on.

This needs RetroArch (and whatever libretro cores/BIOS files your games
need) already installed and configured on the Pi - NostalgiaBox doesn't
manage any of that, it just launches `retroarch` and waits for it to exit.
Sticking to `retroarch`'s own defaults means anything you can normally do
in RetroArch (scan a ROM folder into a playlist, add a system, etc.) just
works here too, without touching `config.yaml` at all.

#### Insights

One of the two evergreen rows at the bottom of the show grid, always present
regardless of what's configured (see Adult Mode above for the other).
**Mute** opens a read-only screen: total minutes watched and episodes
watched; a "favorite" channel (whichever one has the most watched minutes);
a completion bar per channel; a recent-activity feed (most recent first);
and, if the favorite happens to be a well-known title, a couple of
similar-show suggestions as a research pointer for restocking the SD card -
purely text, nothing gets downloaded or added automatically. **Power** backs
out to the grid, same as the episode list. (Games don't show up here -
RetroArch's own menu has no way to report back what got played; see "Games
(arcade)" above.)

---

## Updating later

If a newer version is released:

```bash
cd ~/NostalgiaBox
git pull
sudo systemctl restart nostalgiabox
```

(If you enabled the read-only overlay in Part J, turn it off first via
`raspi-config`, update, then turn it back on.)

---

## Configuration reference (highlights)

All settings live in `config.yaml`:

```yaml
tune_in: random          # random | resume | broadcast
start_channel: 2         # channel to power on to
start_offset: [6, 10]    # start each episode a random 6-10s in (or a fixed number)
transition: none         # channel-change effect: none | glitch | static
bridge_seconds: 0.8      # keep the current show playing while the next loads
channel_bug_seconds: 4   # how long the channel banner lingers
initial_volume: 70       # 0-100
admin_mode_enabled: true # hidden grown-ups view (posters, episode picker, Adult Mode)
admin_hold_seconds: 3.0  # how long to hold Power to open it
admin_seek_seconds: 10.0 # Adult Mode's D-pad seek skip size, 1-300
subtitles_default: false # subtitles on/off at boot; toggled via Info in Adult Mode
audio_device: "..."      # force HDMI audio (see Part H)

ui:                      # the green on-screen display
  color: "#4DFF5A"
  glow: true
crt:                     # the CRT picture effect (curve, rounding, scanlines)
  enabled: true
  curvature: 0.12
```

Leaving out episodes per channel:

```yaml
  - number: 3
    name: "Arthur"
    path: /media/nostalgiabox/arthur
    exclude_seasons: ["6-25"]   # only air seasons 1-5
    exclude: ["*special*"]      # skip the specials
```

Validate any changes with `nostalgiabox --check`.

---

## Troubleshooting

- **`--check` shows 0 episodes for a channel** → the `path` is wrong, or the
  files use an extension not in `video_extensions`.
- **No video on the TV** → make sure the HDMI cable is in the right Pi port and
  the TV is on that input. Check logs with `journalctl -u nostalgiabox -f`.
- **No sound** → see Part H; try switching `vc4hdmi0` ↔ `vc4hdmi1`, or the
  `alsa/plughw:CARD=...` variant.
- **Remote does nothing** → confirm the Flirc is plugged into the Pi and was
  programmed (Part G). Restart the box after plugging it in.
- **It won't boot / config errors after a power cut** → the SD got corrupted from
  an unclean shutdown. Enable the read-only overlay (Part J) to prevent it.

---

## For the curious (how it works)

The project is plain Python. The "brains" (channel scanning, the shuffle, the
state machine) have no hardware dependencies and are fully unit-tested; the
hardware-facing parts (the mpv video player and the remote input) are isolated
behind small interfaces. You can even drive the whole thing on a laptop with a
mock player:

```bash
pip install -e ".[dev]"
pytest
python -m nostalgiabox --dry-run --config config.yaml   # keyboard-controlled, no video
```

```
nostalgiabox/
├── config.py      YAML -> validated config
├── playlist.py    the shuffle bag (each episode once, then reshuffle)
├── channel.py     folder scanning, tune-in modes, channel navigation
├── player.py      mpv player (+ a mock for tests)
├── overlay.py     the on-screen display: retro CRT readouts (channel bug,
│                  volume) and the modern, full-width admin-mode grid/
│                  episode list/Insights
├── crt.py         the CRT shader
├── input/         remote input (Flirc/keyboard, HDMI-CEC, keymap)
├── static_gen.py  ffmpeg-generated static/glitch/colour-bar clips
├── thumbnails.py  ffmpeg+Pillow poster grid for the admin-mode UI
├── watch_state.py per-episode watch history, Continue Watching, Insights
├── recommendations.py  curated similar-show suggestions for Insights
└── app.py         the TV state machine
```

## License

MIT. Enjoy your nostalgia box!
