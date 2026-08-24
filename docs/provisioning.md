# Provisioning the Pi

Setting up a Raspberry Pi Zero 2 W to run DittoBackTracker. About an hour, most
of it waiting on `apt`.

Two things here are unusual: a **separate data partition** and a **read-only
root filesystem**. The device has no battery, so it can lose power the moment
the plug comes out — nothing may be written to the OS card during normal
operation.

End state: a Zero 2 W on WiFi at `dittobacktracker.local`, read-only root, and
the pedal mountable by an unprivileged service.

---

## 1. Image

**Raspberry Pi OS Lite (64-bit)**, Bookworm or Trixie.

64-bit works on the Zero 2 W and avoids the patchy ARMv6 wheel situation. It
costs about 30 MB more RAM out of 512 MB.

In Raspberry Pi Imager, use the gear icon to preconfigure:

| Setting | Value |
|---|---|
| Hostname | `dittobacktracker` |
| Username | `ditto` |
| WiFi SSID and password | your network |
| WiFi country | required, or the radio stays off |
| SSH | enabled |

### Stop the root filesystem auto-expanding

The card needs room for a third partition, so disable the first-boot resize.
Imager ejects the card when it finishes; reinsert it and edit `cmdline.txt` on
the `bootfs` partition before the first boot. On macOS that's
`/Volumes/bootfs/cmdline.txt`; on Linux, mount the partition first.

Look at the file before editing — the token varies by image version. Recent
cloud-init images use a bare `resize`. Delete that token and nothing else:

```text
console=serial0,115200 console=tty1 root=PARTUUID=... rootfstype=ext4
fsck.repair=yes rootwait resize cfg80211.ieee80211_regdom=GB ds=nocloud;i=rpi-imager-...
                         ^^^^^^ delete this
```

Keep it as one line.

**Leave `init=/usr/lib/raspberrypi-sys-mods/firstboot` alone if you see it.**
That entry is what applies the Imager `custom.toml` settings — hostname, user,
WiFi, SSH — on first boot, so removing it costs you the whole configuration.
On an image that uses it rather than a bare `resize`, don't edit `cmdline.txt`
at all: either create the third partition on the card before first boot, or
let the root expand and use the `growpart` override below.

Check after first boot:

```bash
df -h /        # expect ~2.5G, not the full card
```

If it reports the full card size, the resize ran. You cannot shrink a mounted ext4 root, so
reflash rather than trying to recover.

If it expands despite the edit, cloud-init's `growpart` did it. Merge the
following into the `user-data` file on the boot partition — it's a fragment, so
add it to the existing cloud-config rather than replacing the file, and keep
`#cloud-config` as the first line or cloud-init ignores the whole file:

```yaml
#cloud-config
growpart:
  mode: off
resize_rootfs: false
```

Validate the result with `cloud-init schema --config-file user-data` before you
boot.

---

## 2. Data partition

```bash
ssh ditto@dittobacktracker.local
```

`parted` and `usbutils` (for `lsusb`, below) aren't guaranteed on a Lite image,
so install them before you need them:

```bash
sudo apt update && sudo apt install -y parted usbutils
```

The root filesystem is around 2.5 GB and the rest of the card is unallocated.
Check where p2 actually ends before choosing a start point:

```bash
sudo parted /dev/mmcblk0 unit MB print free
```

Then create p3 from just past the end of p2, rounding up. On a Bookworm image
p2 typically ends near 3.2 GB, so:

```bash
sudo parted -a optimal /dev/mmcblk0 --script mkpart primary ext4 3300MB 100%
sudo partprobe /dev/mmcblk0
sudo mkfs.ext4 -L dittodata /dev/mmcblk0p3
sudo mkdir -p /var/lib/ditto
```

Adjust `3300MB` to suit what the previous command reported; parted refuses
overlapping partitions, so a wrong value fails loudly rather than silently. Don't use `0%` as the start — that
means the start of the disk, which is occupied, and parted will pick the small
gap before partition 1 instead.

If `mkfs` runs before the device node appears, p3 will have no filesystem. Run
`mkfs.ext4` again.

```bash
lsblk -o NAME,SIZE,FSTYPE,LABEL /dev/mmcblk0
```

Expect `p3` to fill the rest of the card, ext4, labelled `dittodata`. Around
2 GB is enough; the rest is spare.

Add to `/etc/fstab`:

```text
LABEL=dittodata  /var/lib/ditto  ext4  defaults,noatime  0  2
```

```bash
sudo mount -a
sudo mkdir -p /var/lib/ditto/{sources,staged,trash,app}
```

This partition holds the application, the uploads and the database. It is the
only writable storage once the overlay is on.

Ownership is left alone here on purpose. `install.sh` creates the `ditto-svc`
account in step 6 and hands it the whole partition, so chowning it to the login
user now only means chowning it twice.

---

## 3. Packages

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y \
  git ffmpeg \
  python3-flask python3-waitress \
  avahi-daemon \
  overlayroot
```

Everything from apt, nothing from pip. A read-only root and a virtualenv are an
awkward combination.

There is nothing on the GPIO header, so no I2C, no `i2c-tools` and no group
membership to arrange.

---

## 4. USB host mode

The Zero's data port defaults to host mode. Set it explicitly in
`/boot/firmware/config.txt`:

```ini
dtoverlay=dwc2,dr_mode=host
```

Reboot, plug in the pedal, and check:

```bash
lsusb                          # TC Electronic Ditto Plus
ls -l /dev/disk/by-label/      # DITTOPLUS
```

Udev creates the `by-label` symlink. No custom rule needed.

---

## 5. Mounting

An fstab entry with `noauto,user` lets the service mount the pedal without
root. `install.sh` writes this line; it is here so you can read it:

```text
LABEL=DITTOPLUS  /media/ditto  vfat  noauto,user,rw,flush,fmask=077,dmask=077,uid=ditto-svc,gid=ditto-svc  0  0
```

`fmask=077,dmask=077` keeps the mounted files owner-only (the `ditto-svc`
account the service runs as, not your login) rather than the world-writable
`umask=000`.

```bash
sudo mkdir -p /media/ditto
```

There is nothing to test by hand yet. The entry names `ditto-svc`, and mount
resolves `uid=`/`gid=` when it runs, so until step 6 creates that account the
mount fails with "unknown user" whoever runs it — including root. The hand test
is in step 6, after `install.sh`.

`flush` pushes FAT writes out promptly instead of leaving them in cache.

The pedal stays enumerated after `umount` and does not return to looper
operation while the cable is attached. That is expected — see
[pedal-format.md](pedal-format.md#release-behaviour). Unplug the dongle when
you're done with it.

---

## 6. Install

Clone onto the data partition. Your home directory is on the root filesystem,
which becomes read-only in step 8, so a checkout there can't be updated
afterwards.

```bash
git clone https://github.com/rlhatcher/DittoBackTracker.git /var/lib/ditto/src
cd /var/lib/ditto/src
./install.sh
```

Open `http://dittobacktracker.local/`, plug in the pedal, drop a track in.

The account now exists, so the step 5 mount entry can be tried by hand. Mount
as `ditto-svc`: `user` in fstab lets anyone mount but only the mounting user
unmount, so mounting it as yourself leaves a volume the service cannot release.
Stop the service first, or it will be holding the pedal already.

```bash
sudo systemctl stop ditto-web
sudo -u ditto-svc mount /media/ditto
ls /media/ditto          # 01track/ … 99track/
sudo -u ditto-svc umount /media/ditto
sudo systemctl start ditto-web
```

Do this before enabling the overlay in step 8. `install.sh` writes to `/etc`,
and those changes are discarded once the root filesystem is read-only.

The installer is idempotent, and repeats the package install and fstab entry
from steps 3 and 5. It does not install `overlayroot`, so don't skip step 3.

---

## 7. Boot time

Stock boot is 20–40 s, and the device boots every time you use it. Around 10 s
is achievable.

```bash
sudo systemctl disable --now \
  triggerhappy.service \
  keyboard-setup.service \
  apt-daily.timer apt-daily-upgrade.timer \
  man-db.timer \
  dphys-swapfile.service \
  bluetooth.service hciuart.service
```

`dphys-swapfile` has to go regardless — swap on a read-only root makes no
sense.

In `/boot/firmware/config.txt`:

```ini
disable_splash=1
dtoverlay=disable-bt
boot_delay=0
```

Measure with `systemd-analyze` and `systemd-analyze blame | head -15`.

Do this before step 8. Each attempt needs a writable root.

---

## 8. Read-only root filesystem

Last, once everything above works.

Stop journald writing to the card:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=16M\n' \
  | sudo tee /etc/systemd/journald.conf.d/volatile.conf
```

Then set `/etc/overlayroot.conf`:

```text
overlayroot="tmpfs:recurse=0"
```

`recurse=0` is not optional. Without it, overlayroot also overlays the data
partition: writes to `/var/lib/ditto` go to RAM and disappear on reboot, with
no error. With it, only the root filesystem is overlaid.

```bash
sudo reboot
```

Verify:

```bash
mount | grep ' / '            # overlay
df -h /var/lib/ditto          # must be /dev/mmcblk0p3, not an overlay
sudo -u ditto-svc touch /var/lib/ditto/x && echo ok && sudo rm /var/lib/ditto/x
```

The write test runs as `ditto-svc` because that is the account that has to be
able to write there. Your login user cannot, and that is the point.

### Changing anything afterwards

Application code lives on `/var/lib/ditto/app`, which stays writable, so
updating is:

```bash
sudo -u ditto-svc git -C /var/lib/ditto/src pull
sudo -u ditto-svc rm -rf /var/lib/ditto/app/ditto
sudo -u ditto-svc cp -r /var/lib/ditto/src/ditto /var/lib/ditto/app/
sudo systemctl restart ditto-web
```

The `rm` matters: without it, modules deleted upstream linger in the deployed
copy.

Every step runs as `ditto-svc` because the data partition belongs to it. Run
the `git pull` as yourself and git refuses with "detected dubious ownership"
rather than doing something half-right.

Or press **Update** in the web UI for the same result over the air — how it works
is in [api.md](api.md#post-apiupdate). It needs the `ditto-restart.service`
unit and sudoers rule that `install.sh` installs; if you provisioned before those
existed, re-run `install.sh` once with the overlay disabled (below) to add them.

An update over the air replaces the Python and nothing else. It does not re-run
`install.sh`, so a device that self-updates past 0.4.0 keeps running as whatever
account it was installed with. Moving it to `ditto-svc` takes one `install.sh`
run with the overlay off.

For changes under `/etc`:

```bash
sudo overlayroot-chroot        # writes land on the real root filesystem
```

Or disable the overlay for a session. Note this edit has to happen inside the
chroot, since editing the file normally writes to the discarded tmpfs layer:

```bash
sudo overlayroot-chroot
sed -i 's/^overlayroot=.*/overlayroot=""/' /etc/overlayroot.conf
exit
sudo reboot
```

Re-enable it once you're done. The overlay is off now, so the root is writable
and you edit `/etc/overlayroot.conf` directly — no chroot:

```bash
sudo sed -i 's/^overlayroot=.*/overlayroot="tmpfs:recurse=0"/' /etc/overlayroot.conf
sudo reboot
```

### Upgrading a device that is already running

Most releases need none of this: press **Update** and the device redeploys
itself. Do this only when a release changes something outside the `ditto/`
package — the unit, the sudoers rules, the fstab entry or the ownership of
`/var/lib/ditto`. An over-the-air update copies the Python and nothing else,
so those stay as they were until `install.sh` runs again, and `install.sh`
writes to `/etc`, which means the overlay has to be off for one boot.

0.4.0 is such a release: it moves the service off your login account onto
`ditto-svc`. Two reboots, and nothing on the pedal is touched.

**1. Finish the session.** Press **Done** and unplug the pedal, so nothing is
mid-write when the service stops. Power the Pi back up.

**2. Turn the overlay off for a boot.** The edit has to happen inside the
chroot or it lands on the tmpfs layer and is discarded:

```bash
sudo overlayroot-chroot
sed -i 's/^overlayroot=.*/overlayroot=""/' /etc/overlayroot.conf
exit
sudo reboot
```

**3. Pull and install.** Which `git pull` depends on who owns the checkout,
which depends on whether this device has been through the 0.4.0 migration
already. Coming from 0.3.x it is still your login account:

```bash
cd /var/lib/ditto/src
git pull
./install.sh
```

On a device already running 0.4.0 or later, the checkout belongs to
`ditto-svc` and git refuses it as anyone else:

```bash
cd /var/lib/ditto/src
sudo -u ditto-svc git pull
./install.sh
```

`install.sh` creates the account, stops the service, unmounts the pedal if it
is still mounted, hands `/var/lib/ditto` to `ditto-svc`, rewrites the fstab
entry and both sudoers rules, replaces the unit and starts it again. It is
idempotent, so a second run costs nothing.

If it fails it says which state it left the device in. "Restarting the service
that was running" means nothing changed hands and you are back where you
started. A part-migrated message means the data moved but the unit did not, and
the service is deliberately left down — fix what it reported and run it again.

**4. Check it took**, before putting the overlay back:

```bash
systemctl show ditto-web -p User        # User=ditto-svc
id ditto-svc                            # no sudo group
systemctl is-active ditto-web           # active
```

Then open the page, plug the pedal in, confirm the slot map fills, and press
**Done** to confirm the poweroff rule still matches. That last one is the
easiest to get wrong and the least obvious when it is: a refused poweroff
arrives after the page has already said it is safe to unplug.

**Done halts the Pi**, which is how you know the rule worked. Power it back up
for the last step.

**5. Put the overlay back.** Root is writable now, so no chroot:

```bash
sudo sed -i 's/^overlayroot=.*/overlayroot="tmpfs:recurse=0"/' /etc/overlayroot.conf
sudo reboot
```

From here on, updates that only touch the Python go over the air again.

---

## 9. Optional: measure write throughput

Not needed. Useful if you want a number for your own card and cable.

With the pedal mounted. The trailing `sync` is required or you measure the page
cache:

```bash
time { dd if=/dev/zero of=/media/ditto/speed.bin bs=1M count=50; sync; }
rm /media/ditto/speed.bin
```

Expect roughly 1 MB/s. That figure is what sizes the loop-staging timeout in
`config.LOOP_STAGE_TIMEOUT`.

---

## Checklist

- [ ] `dittobacktracker.local` resolves from your laptop
- [ ] Pedal appears at `/dev/disk/by-label/DITTOPLUS` when connected
- [ ] `id ditto-svc` shows it is **not** in the `sudo` group
- [ ] `ditto-svc` can mount and unmount the pedal without `sudo`
- [ ] `systemctl status ditto-web` is active, with no restart loop
- [ ] The web UI loads and shows the slot grid
- [ ] A dropped MP3 converts and plays back from the pedal
- [ ] Boot to SSH in under 15 s
- [ ] `df -h /var/lib/ditto` shows `/dev/mmcblk0p3`, not an overlay
- [ ] Ten hard power cuts leave no fsck and nothing corrupt on the pedal

The `df` check is the one people miss. If the data partition is overlaid,
everything works until the first reboot, then every upload is gone and nothing
reported an error.
