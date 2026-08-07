#!/usr/bin/env bash
# Push app code to a running DittoBackTracker over the air (SSH).
#
# The app is pure Python on the writable data partition, so a deploy is just
# "replace the code and restart the service" — no build, and none of the
# read-only-root/overlay dance, because nothing here touches /etc. It mirrors
# the manual update in docs/provisioning.md (§9): remove the old package, drop
# the new one in, restart.
#
# It syncs the `ditto/` package ONLY. A change to the systemd unit, sudoers, or
# anything else under /etc is a provisioning change — do that with
# `sudo overlayroot-chroot`, not this script.
#
# Usage:
#   ./deploy.sh                       # -> ditto@dittobacktracker.local
#   ./deploy.sh -h pi.local -u ditto  # override host / user
#   ./deploy.sh --no-restart          # copy only, leave the service running old code
#
# Requires: ssh on this machine, and tar on both ends (always present). No
# rsync needed. SSH key auth is assumed; the restart uses `ssh -t` so its one
# sudo prompt works if the ditto user still needs a password for systemctl.
set -euo pipefail

HOST=dittobacktracker.local
USER=ditto
RESTART=1

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--host) HOST="$2"; shift 2 ;;
    -u|--user) USER="$2"; shift 2 ;;
    --no-restart) RESTART=0; shift ;;
    --help)
      sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$USER@$HOST"
APP=/var/lib/ditto/app

if [ ! -d "$HERE/ditto" ]; then
  echo "error: no ditto/ package next to this script." >&2
  exit 1
fi

echo "==> deploying ditto/ to $DEST:$APP/ditto"
# One SSH: on the far end, remove the old package (so modules deleted upstream
# don't linger) then unpack the new one from stdin. --exclude keeps local
# bytecode out of the transfer.
# shellcheck disable=SC2029  # $APP is a fixed local constant; expanding it
# client-side into the remote command is exactly what we want here.
tar -C "$HERE" --exclude='__pycache__' --exclude='*.pyc' -czf - ditto \
  | ssh "$DEST" "rm -rf $APP/ditto && mkdir -p $APP && tar -C $APP -xzf -"

if [ "$RESTART" -eq 1 ]; then
  echo "==> restarting ditto-web (graceful: drains the queue, unmounts cleanly)"
  ssh -t "$DEST" 'sudo systemctl restart ditto-web'
  # is-active returns non-zero if the unit failed to come back up.
  if ssh "$DEST" 'systemctl is-active --quiet ditto-web'; then
    echo "Deployed. http://$HOST/"
  else
    echo "Service is not active after restart. Check:" >&2
    echo "  ssh $DEST 'journalctl -u ditto-web -n 30 --no-pager'" >&2
    exit 1
  fi
else
  echo "Code updated. Restart when ready:  ssh $DEST 'sudo systemctl restart ditto-web'"
fi
