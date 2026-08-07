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
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [ ! -d /var/lib/ditto ]; then
  echo "error: /var/lib/ditto does not exist." >&2
  echo "Work through docs/provisioning.md first — the data partition is" >&2
  echo "created there, and without it nothing persists." >&2
  exit 1
fi

echo "==> packages"
sudo apt-get update -qq
sudo apt-get install -y ffmpeg python3-flask python3-waitress \
                        python3-smbus python3-gpiozero i2c-tools avahi-daemon

echo "==> code -> $APP"
mkdir -p "$APP"
rm -rf "$APP/ditto"
cp -r "$HERE/ditto" "$APP/"
sudo chown -R ditto:ditto /var/lib/ditto

echo "==> allow unprivileged poweroff (for the Done button)"
sudo install -m 0440 "$HERE/etc/99-ditto-poweroff" /etc/sudoers.d/99-ditto-poweroff

echo "==> pedal mount entry"
if ! grep -q DITTOPLUS /etc/fstab; then
  echo 'LABEL=DITTOPLUS  /media/ditto  vfat  noauto,user,rw,flush,umask=000,uid=ditto,gid=ditto  0  0' \
    | sudo tee -a /etc/fstab >/dev/null
fi
sudo mkdir -p /media/ditto

echo "==> service"
sudo cp "$HERE/systemd/ditto-web.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl reset-failed ditto-web 2>/dev/null || true
sudo systemctl enable --now ditto-web

sleep 2
echo
if systemctl is-active --quiet ditto-web; then
  echo "Running.  http://$(hostname).local/"
else
  echo "Service did not start. Check:  journalctl -u ditto-web -n 30 --no-pager" >&2
  exit 1
fi
