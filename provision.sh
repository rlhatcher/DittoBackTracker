#!/usr/bin/env bash
# Prepare a freshly imaged Raspberry Pi for DittoBackTracker.
#
# Covers steps 2, 3, 4 and 7 of docs/provisioning.md: the data partition,
# packages, USB host mode and boot time. Step 1 happens on your laptop before
# the Pi first boots. Step 8, the read-only root, is deliberately left out: it
# should go on once the device is known to work, and taking it off again needs
# a chroot.
#
# Idempotent: safe to re-run, and safe on a machine that is part-way through.
# It never reformats a partition that already holds a filesystem.
set -euo pipefail

DISK=/dev/mmcblk0
ROOT_PART=2
DATA_PART=3
# The root has to hold ffmpeg and its dependency stack, which the stock 2.3 GB
# image cannot: a fresh Trixie Lite root has about 370 MB free, and step 3 needs
# more than that. 8 GB leaves room for the packages and for future upgrades on a
# device whose root is about to become read-only and awkward to grow.
ROOT_SIZE=8GB
DATA_LABEL=dittodata
DATA_MOUNT=/var/lib/ditto
BOOT_CONFIG=/boot/firmware/config.txt

say() { echo "==> $*"; }

if [ ! -b "$DISK" ]; then
  echo "error: $DISK not found. This script is for a Raspberry Pi booted from" >&2
  echo "an SD card. See docs/provisioning.md." >&2
  exit 1
fi

# Everything below writes to /etc or /boot, both of which are discarded once
# overlayroot is on. Refuse rather than appear to work.
if mount | grep -q 'on / .*overlay'; then
  echo "error: the read-only overlay is active, so changes to /etc and /boot" >&2
  echo "would be discarded on the next reboot. Disable it for one boot first:" >&2
  echo "  docs/provisioning.md, 'Changing anything afterwards'." >&2
  exit 1
fi

say "tools"
# Reclaim first. The root is the thing that is short of space, and a run that
# died part-way through step 3 leaves its downloads in the apt cache, which is
# exactly the space this needs to get as far as growing the root.
sudo apt-get clean
# parted partitions, growpart grows the root in place, usbutils provides lsusb.
sudo apt-get update -qq
sudo apt-get install -y parted usbutils cloud-guest-utils

# --- Step 2: the data partition -------------------------------------------
#
# Order matters. The data partition is created first, at ROOT_SIZE, and the
# root is then grown into the gap left in front of it. growpart stops at the
# next partition, so that gap is what bounds the root, and there is never a
# moment where the root has taken the whole card.
#
# Doing it the other way round means resizing a mounted partition with parted,
# which prompts because the partition is in use and cannot be scripted without
# faking a terminal. growpart exists for exactly this and is what cloud-init
# runs on a mounted root on every Pi that expands itself on first boot.

if [ -b "${DISK}p${DATA_PART}" ]; then
  say "data partition already exists"
else
  say "data partition -> ${DISK}p${DATA_PART}"
  sudo parted -a optimal "$DISK" --script \
       mkpart primary ext4 "$ROOT_SIZE" 100%
  sudo partprobe "$DISK"
  # udev may not have made the node by the time parted returns, and mkfs on a
  # missing node fails the run rather than silently doing nothing.
  for _ in $(seq 1 20); do
    [ -b "${DISK}p${DATA_PART}" ] && break
    sleep 0.5
  done
  if [ ! -b "${DISK}p${DATA_PART}" ]; then
    echo "error: ${DISK}p${DATA_PART} did not appear after partprobe." >&2
    exit 1
  fi
fi

say "root filesystem -> $ROOT_SIZE"
# growpart exits 1 and prints NOCHANGE when there is nothing to do, which is
# the normal case on a re-run. Anything else is a real failure.
if ! out=$(sudo growpart "$DISK" "$ROOT_PART" 2>&1); then
  case "$out" in
    *NOCHANGE*) echo "   already grown" ;;
    *) echo "$out" >&2; exit 1 ;;
  esac
else
  echo "$out"
fi
# No-op when the filesystem already fills the partition.
sudo resize2fs "${DISK}p${ROOT_PART}"

# Only ever format a partition with nothing on it. A re-run must not take the
# library and the database with it.
if [ -n "$(sudo blkid -o value -s TYPE "${DISK}p${DATA_PART}" 2>/dev/null)" ]; then
  say "data partition already has a filesystem, leaving it alone"
else
  say "formatting ${DISK}p${DATA_PART}"
  sudo mkfs.ext4 -L "$DATA_LABEL" "${DISK}p${DATA_PART}"
fi

say "mount entry for $DATA_MOUNT"
FSTAB_LINE="LABEL=$DATA_LABEL  $DATA_MOUNT  ext4  defaults,noatime  0  2"
if grep -q "LABEL=$DATA_LABEL" /etc/fstab && ! grep -qF "$FSTAB_LINE" /etc/fstab; then
  echo "   replacing an outdated $DATA_LABEL entry"
  sudo sed -i.ditto-bak "\|LABEL=$DATA_LABEL|d" /etc/fstab
fi
if ! grep -qF "$FSTAB_LINE" /etc/fstab; then
  echo "$FSTAB_LINE" | sudo tee -a /etc/fstab >/dev/null
fi
sudo mkdir -p "$DATA_MOUNT"
mountpoint -q "$DATA_MOUNT" || sudo mount "$DATA_MOUNT"
sudo mkdir -p "$DATA_MOUNT"/{sources,staged,trash,app}
# Ownership is left alone: install.sh creates ditto-svc and takes the partition.

# --- Step 3: packages ------------------------------------------------------
#
# No full-upgrade. It is a few hundred MB of churn on a freshly imaged card
# that is about to become read-only, and none of it is needed to run this.
say "packages"
sudo apt-get install -y \
  git ffmpeg \
  python3-flask python3-waitress \
  avahi-daemon \
  overlayroot
sudo apt-get clean

# --- Step 4: USB host mode -------------------------------------------------
say "USB host mode"
add_boot_config() {
  grep -qxF "$1" "$BOOT_CONFIG" || echo "$1" | sudo tee -a "$BOOT_CONFIG" >/dev/null
}
add_boot_config "dtoverlay=dwc2,dr_mode=host"

# --- Step 7: boot time -----------------------------------------------------
say "boot time"
# Not all of these are present on every image, so disable them one at a time
# and let a missing unit pass. dphys-swapfile is the one that matters most:
# swap on a read-only root cannot work.
for unit in triggerhappy.service keyboard-setup.service \
            apt-daily.timer apt-daily-upgrade.timer \
            man-db.timer dphys-swapfile.service \
            bluetooth.service hciuart.service; do
  sudo systemctl disable --now "$unit" 2>/dev/null || true
done
add_boot_config "disable_splash=1"
add_boot_config "dtoverlay=disable-bt"
add_boot_config "boot_delay=0"

echo
say "done"
df -h / "$DATA_MOUNT"
echo
echo "Reboot for USB host mode and the boot settings, then:"
echo "  git clone https://github.com/rlhatcher/DittoBackTracker.git $DATA_MOUNT/src"
echo "  cd $DATA_MOUNT/src && ./install.sh"
echo
echo "Enable the read-only root (step 8) last, once the pedal and a track have"
echo "been through it."
