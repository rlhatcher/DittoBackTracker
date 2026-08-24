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
# The account the service runs as, and deliberately not the login account. The
# Raspberry Pi Imager user is in the sudo group, and POST /api/update executes
# code from the tracked branch, so a service running as it hands root to anyone
# on the LAN. This account gets no shell and no sudo group: the two rules in
# etc/ are the whole of what it may do as root.
SVC=ditto-svc
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Root-writable, not caller-writable: after the first run the data partition
# belongs to $SVC and the admin is not in that group. mountpoint is the half
# that matters anyway — it catches an overlay having swallowed the partition.
if ! mountpoint -q /var/lib/ditto || ! sudo test -w /var/lib/ditto; then
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

echo "==> service account: $SVC"
if ! id -u "$SVC" >/dev/null 2>&1; then
  # No shell and no home of its own: /var/lib/ditto is where it works, not
  # where it lives. --user-group so the fstab gid= has a group to name.
  sudo useradd --system --user-group --home-dir /var/lib/ditto \
       --no-create-home --shell /usr/sbin/nologin "$SVC"
fi

# Everything from here to the restart at the end can exit non-zero, and from
# here on the service is down. Put it back if we fail, rather than leaving a
# device with no web UI -- the likeliest failure is the checkout validation
# below, which is exactly the case where someone is already mid-problem. A
# device that was already stopped stays stopped.
WAS_ACTIVE=0
if systemctl is-active --quiet ditto-web 2>/dev/null; then
  WAS_ACTIVE=1
fi
restore_service() {
  local status=$?
  if [ "$status" -ne 0 ] && [ "$WAS_ACTIVE" -eq 1 ]; then
    echo >&2
    echo "install failed; restarting the service that was running before" >&2
    sudo systemctl start ditto-web || true
  fi
}
trap restore_service EXIT

# Stop before touching ownership. A recursive chown under a live SQLite writer
# can leave a half-owned WAL, and on a device migrating off the old account the
# service is still running as the wrong user right now.
sudo systemctl stop ditto-web 2>/dev/null || true
# The pedal's fstab entry carries uid=/gid=, rewritten below. `user` in fstab
# lets any user mount but only the mounting user unmount, so a volume the old
# account mounted cannot be released by the new one. Root can, so do it here
# rather than leaving a stuck mount for the first session to trip over.
if mountpoint -q /media/ditto; then
  sudo umount /media/ditto
fi

echo "==> ownership -> $SVC"
# Before the git steps, not after. Once the data partition belongs to $SVC, git
# refuses to touch $SRC as anyone else ("detected dubious ownership"), so every
# git call and every write below has to run as $SVC. Doing this last, as it
# used to, worked on a fresh install and failed on every re-run.
sudo chown -R "$SVC:$SVC" /var/lib/ditto

# Validate the checkout OTA will pull from. This runs after the package step so a
# device that doesn't have git yet still gets a clear result rather than a bare
# "git: command not found".
if ! sudo -u "$SVC" git -C "$SRC" rev-parse --is-inside-work-tree >/dev/null 2>&1
then
  echo "error: $SRC is not a valid git checkout, so over-the-air updates" >&2
  echo "can't pull. Clone the repo to $SRC rather than copying it." >&2
  exit 1
fi
if ! sudo -u "$SVC" git -C "$SRC" remote get-url origin >/dev/null 2>&1; then
  echo "error: $SRC has no 'origin' remote, so over-the-air updates can't" >&2
  echo "fetch. Clone it from your GitHub remote to $SRC." >&2
  exit 1
fi

echo "==> code -> $APP"
sudo -u "$SVC" mkdir -p "$APP"
sudo -u "$SVC" rm -rf "$APP/ditto"
sudo -u "$SVC" cp -r "$HERE/ditto" "$APP/"
# Record the deployed commit so the app reports its revision and the update
# check has a baseline. Harmless if this checkout isn't a git repo. Piped
# through tee because the redirect would run as the caller, who cannot write
# into $APP any more.
if sudo -u "$SVC" git -C "$HERE" rev-parse HEAD >/dev/null 2>&1; then
  sudo -u "$SVC" git -C "$HERE" rev-parse HEAD \
    | sudo -u "$SVC" tee "$APP/REVISION" >/dev/null
fi

# Check both rules parse before installing either. A malformed file in
# /etc/sudoers.d breaks sudo for every user on a device whose root filesystem
# is about to go read-only, and the app's only privileged calls go through it.
echo "==> allow unprivileged poweroff (for the Done button)"
sudo visudo -c -f "$HERE/etc/99-ditto-poweroff"
sudo install -m 0440 "$HERE/etc/99-ditto-poweroff" /etc/sudoers.d/99-ditto-poweroff

echo "==> allow unprivileged restart (for over-the-air self-update)"
sudo visudo -c -f "$HERE/etc/99-ditto-restart"
sudo install -m 0440 "$HERE/etc/99-ditto-restart" /etc/sudoers.d/99-ditto-restart

echo "==> pedal mount entry"
# Owner-only masks: the pedal's files shouldn't be world-readable/writable.
FSTAB_LINE="LABEL=DITTOPLUS  /media/ditto  vfat  noauto,user,rw,flush,fmask=077,dmask=077,uid=$SVC,gid=$SVC  0  0"
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
