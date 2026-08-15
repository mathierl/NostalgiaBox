#!/usr/bin/env bash
#
# Remove Kodi and its game-client add-ons from the Pi.
#
# UKE-28 originally planned to use Kodi as the arcade engine, but after a
# night of hands-on testing that plan was dropped in favour of launching
# RetroArch directly from admin mode (see the ticket for why). Kodi is no
# longer part of the plan, so this reclaims the space it and its game-related
# packages were using.
#
# Deliberately NOT removed: `retroarch` and `libretro-snes9x` (apt packages)
# and the PS1 core fetched by hand into ~/.config/retroarch/cores/ - those
# are still needed by the new plan.
#
# Usage:
#   ./scripts/remove-kodi.sh
#
set -euo pipefail

echo "==> Removing Kodi and its game-client add-ons"
sudo apt-get purge -y kodi kodi-game-libretro kodi-peripheral-joystick
sudo apt-get autoremove -y

echo "==> Kodi's config/userdata/cache directory (~/.kodi) is untouched by apt purge"
echo "    and can be sizeable (thumbnails, addon data). Remove it too? [y/N]"
read -r answer
if [[ "${answer}" =~ ^[Yy]$ ]]; then
  rm -rf ~/.kodi
  echo "    removed ~/.kodi"
else
  echo "    left in place - remove later with: rm -rf ~/.kodi"
fi

echo "==> Done. retroarch and libretro-snes9x are still installed - not touched by this script."
