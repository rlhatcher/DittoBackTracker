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

# Validate the checkout OTA will pull from, before anything is stopped or
# chowned, so a bad checkout costs nothing. This runs after the package step so
# a device without git yet gets a clear result rather than "git: command not
# found".
#
# As $SRC's current owner, whoever that is: git refuses a work tree owned by
# someone else ("detected dubious ownership"). On a first install that owner is
# root, because /var/lib/ditto belongs to root until the chown below and the
# clone therefore needs sudo. On a re-run it is $SVC.
SRC_OWNER="$(stat -c '%U' "$SRC")"
if ! sudo -u "$SRC_OWNER" git -C "$SRC" rev-parse --is-inside-work-tree \
     >/dev/null 2>&1; then
  echo "error: $SRC is not a valid git checkout, so over-the-air updates" >&2
  echo "can't pull. Clone the repo to $SRC rather than copying it." >&2
  exit 1
fi
if ! sudo -u "$SRC_OWNER" git -C "$SRC" remote get-url origin >/dev/null 2>&1
then
  echo "error: $SRC has no 'origin' remote, so over-the-air updates can't" >&2
  echo "fetch. Clone it from your GitHub remote to $SRC." >&2
  exit 1
fi

# From here on the service is down, and everything below can exit non-zero.
# Put it back if we fail, rather than leaving a device with no web UI. A device
# that was already stopped stays stopped.
#
# Only until the chown, though. Past that the data partition belongs to $SVC
# while the installed unit still names the old account, so starting it would
# restore a service that cannot write its own database -- a restart loop, and
# more confusing than being down. Say what state the device is in instead.
WAS_ACTIVE=0
if systemctl is-active --quiet ditto-web 2>/dev/null; then
  WAS_ACTIVE=1
fi
# How far in we got, so the trap knows what is safe to do about it.
STAGE=stopped
restore_service() {
  local status=$?
  [ "$status" -eq 0 ] && return 0
  case "$STAGE" in
    stopped)
      # Nothing has changed hands yet, so what was running is still coherent.
      if [ "$WAS_ACTIVE" -eq 1 ]; then
        echo >&2
        echo "install failed; restarting the service that was running" >&2
        sudo systemctl start ditto-web || true
      fi
      ;;
    chowning)
      echo >&2
      echo "install failed after /var/lib/ditto changed hands: the data now" >&2
      echo "belongs to $SVC and the service is not yet set up to match." >&2
      echo "Nothing is started, because starting it would only fail on every" >&2
      echo "write. Fix what failed above and run install.sh again." >&2
      ;;
    installed)
      : # the unit matches the data; the message below this line is better
      ;;
  esac
}
trap restore_service EXIT

# Stop before touching ownership: a recursive chown under a live SQLite writer
# can leave a half-owned WAL. A no-op on a first install, and the reason a
# re-install over a running device is safe.
sudo systemctl stop ditto-web 2>/dev/null || true
# The pedal's fstab entry carries uid=/gid= and is rewritten below, so anything
# mounted under the old options has to come down first. `user` in fstab lets
# any user mount but only the mounting user unmount; root is not bound by that,
# so do it here rather than leave a stuck mount for the next session.
if mountpoint -q /media/ditto; then
  sudo umount /media/ditto
fi

echo "==> ownership -> $SVC"
# Everything below this line writes as $SVC, because from here $SVC owns the
# tree and git refuses a work tree owned by anyone else.
#
# The stage is set before the chown rather than after. `chown -R` reports what
# it could not change, carries on with the rest, and exits non-zero -- so a
# partial failure trips set -e with much of the tree already moved. Setting it
# afterwards would leave the trap believing nothing had changed hands, and
# starting a service onto data it does not own is the exact case this variable
# exists to prevent.
STAGE=chowning
sudo chown -R "$SVC:$SVC" /var/lib/ditto

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
# Unit and data agree from here; the script owns the restart below.
STAGE=installed
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
