#!/usr/bin/env bash
# Install DittoBackTracker on a provisioned Raspberry Pi.
#
# Run from a checkout, ideally on the data partition (/var/lib/ditto/src) so it
# stays writable once the root filesystem is read-only.
#
# Idempotent: safe to re-run. But re-running after the overlay is enabled will
# appear to work while silently discarding its changes to /etc.
#
# See docs/provisioning.md.
set -euo pipefail

APP=/var/lib/ditto/app
SRC=/var/lib/ditto/src        # must match config.SRC — the checkout OTA pulls
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if ! mountpoint -q /var/lib/ditto || [ ! -w /var/lib/ditto ]; then
  echo "error: /var/lib/ditto is not a mounted, writable data partition." >&2
  echo "Work through docs/provisioning.md first — the data partition is" >&2
  echo "created there. A bare directory on the root/overlay would lose the" >&2
  echo "app and state.db on the next reboot." >&2
  exit 1
fi

# Over-the-air updates pull the git checkout at $SRC, so installing from anywhere
# else would leave a device whose Update button always fails "no git checkout at
# /var/lib/ditto/src". Require the documented location (provisioning.md step 6).
if [ "$HERE" != "$SRC" ]; then
  echo "error: run install.sh from the checkout at $SRC (not $HERE), so" >&2
  echo "over-the-air updates work. See docs/provisioning.md step 6:" >&2
  echo "  git clone <url> $SRC && cd $SRC && ./install.sh" >&2
  exit 1
fi
echo "==> packages"
sudo apt-get update -qq
sudo apt-get install -y git ffmpeg python3-flask python3-waitress avahi-daemon

# Validate the checkout OTA will pull from. This runs after the package step so a
# device that doesn't have git yet still gets a clear result rather than a bare
# "git: command not found".
if ! git -C "$SRC" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "error: $SRC is not a valid git checkout, so over-the-air updates" >&2
  echo "can't pull. Clone the repo to $SRC rather than copying it." >&2
  exit 1
fi
if ! git -C "$SRC" remote get-url origin >/dev/null 2>&1; then
  echo "error: $SRC has no 'origin' remote, so over-the-air updates can't" >&2
  echo "fetch. Clone it from your GitHub remote to $SRC." >&2
  exit 1
fi

echo "==> code -> $APP"
mkdir -p "$APP"
rm -rf "$APP/ditto"
cp -r "$HERE/ditto" "$APP/"
# Record the deployed commit so the app reports its revision and the update
# check has a baseline. Harmless if this checkout isn't a git repo.
if git -C "$HERE" rev-parse HEAD >/dev/null 2>&1; then
  git -C "$HERE" rev-parse HEAD > "$APP/REVISION"
fi
sudo chown -R ditto:ditto /var/lib/ditto

echo "==> allow unprivileged poweroff (for the Done button)"
sudo install -m 0440 "$HERE/etc/99-ditto-poweroff" /etc/sudoers.d/99-ditto-poweroff

echo "==> allow unprivileged restart (for over-the-air self-update)"
sudo install -m 0440 "$HERE/etc/99-ditto-restart" /etc/sudoers.d/99-ditto-restart

echo "==> pedal mount entry"
# Owner-only masks: the pedal's files shouldn't be world-readable/writable.
FSTAB_LINE='LABEL=DITTOPLUS  /media/ditto  vfat  noauto,user,rw,flush,fmask=077,dmask=077,uid=ditto,gid=ditto  0  0'
if grep -q DITTOPLUS /etc/fstab && ! grep -qF "$FSTAB_LINE" /etc/fstab; then
  echo "   replacing an outdated DITTOPLUS fstab entry with the secure one"
  sudo sed -i.ditto-bak '\|LABEL=DITTOPLUS|d' /etc/fstab
fi
if ! grep -qF "$FSTAB_LINE" /etc/fstab; then
  echo "$FSTAB_LINE" | sudo tee -a /etc/fstab >/dev/null
fi
sudo mkdir -p /media/ditto

echo "==> service"
sudo cp "$HERE/systemd/ditto-web.service" /etc/systemd/system/
# OTA restart helper: started on demand after a self-update, not enabled at boot.
sudo cp "$HERE/systemd/ditto-restart.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl reset-failed ditto-web 2>/dev/null || true
# enable (create the boot symlink) then restart, so re-installing over a running
# service actually loads the new code. `enable --now` no-ops on an already-active
# unit and would leave the old process running.
sudo systemctl enable ditto-web
sudo systemctl restart ditto-web

sleep 2
echo
if systemctl is-active --quiet ditto-web; then
  echo "Running.  http://$(hostname).local/"
else
  echo "Service did not start. Check:  journalctl -u ditto-web -n 30 --no-pager" >&2
  exit 1
fi
