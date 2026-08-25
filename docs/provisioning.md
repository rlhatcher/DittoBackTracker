# Provisioning the Pi

End state: a Zero 2 W on WiFi at `dittobacktracker.local`, read-only root, and
the pedal mountable by an unprivileged service. The [checklist](#checklist)
verifies it.

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
`bootfs` before the first boot — `/Volumes/bootfs/cmdline.txt` on macOS, or
mount the partition first on Linux.

Read the file before editing: the token varies by image version. Recent
cloud-init images use a bare `resize`. Delete that and nothing else:

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

If it reports the full card size the resize ran. A mounted ext4 root cannot be
shrunk, so reflash rather than try to recover.

If it expands despite the edit, cloud-init's `growpart` did it. Merge this
fragment into the existing `user-data` on the boot partition rather than
replacing the file, keeping `#cloud-config` as the first line or cloud-init
ignores all of it:

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

Adjust `3300MB` to what the previous command reported. Parted refuses
overlapping partitions, so a wrong value fails rather than corrupts. Don't use
`0%` as the start: that means the start of the disk, which is occupied, and
parted picks the small gap before partition 1 instead.

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

Leave the ownership alone. `install.sh` creates the `ditto-svc` account in
step 6 and takes the partition then.

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

Everything from apt, nothing from pip: a read-only root and a virtualenv are an
awkward combination.

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

`fmask=077,dmask=077` keeps the mounted files owner-only — owned by
`ditto-svc`, the account the service runs as, not your login — rather than the
world-writable `umask=000`. `flush` pushes FAT writes out promptly instead of
leaving them in cache.

```bash
sudo mkdir -p /media/ditto
```

Don't try mounting it yet. `mount` resolves `uid=`/`gid=` when it runs, so
until step 6 creates `ditto-svc` this fails with "unknown user" for everyone,
root included.

The pedal stays enumerated after `umount` and won't return to looper operation
while the cable is attached. That is expected, and unplugging is the only way
back — see [pedal-format.md](pedal-format.md#release-behaviour).

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

Run this before step 8. `install.sh` writes to `/etc`, and those writes are
discarded once the root filesystem is read-only.

It installs the packages, creates the `ditto-svc` account the service runs as,
gives it `/var/lib/ditto`, writes the fstab entry and both sudoers rules,
deploys the code and starts the unit. Idempotent: re-running it is how a change
to any of those is applied. It repeats steps 3 and 5 but does not install
`overlayroot`, so don't skip step 3.

Open `http://dittobacktracker.local/`, plug in the pedal, drop a track in.

The account exists now, so the step 5 entry can be tried by hand. Every line
runs as `ditto-svc`, including the `ls`: `dmask=077` makes the mount `0700`
owned by that account, and `user` in fstab lets anyone mount but only the
mounting user unmount, so doing it as yourself leaves a volume the service
cannot release.

```bash
sudo systemctl stop ditto-web
sudo -u ditto-svc mount /media/ditto
sudo -u ditto-svc ls /media/ditto      # 01track/ … 99track/
sudo -u ditto-svc umount /media/ditto
sudo systemctl start ditto-web
```

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
able to write there. Your login user cannot.

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

Or press **Update** in the web UI for the same result over the air — how it
works is in [api.md](api.md#post-apiupdate).

Either way, only the `ditto/` package moves. A release that also changes the
systemd unit, the sudoers rules, the fstab entry or the ownership of
`/var/lib/ditto` needs `install.sh` run again, and `install.sh` writes to
`/etc`, so the overlay has to come off for that boot — below.

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

---

## 9. Optional: measure write throughput

Not needed. Useful if you want a number for your own card and cable.

With the pedal mounted, as `ditto-svc` — the volume is owner-only, so your
login user cannot write to it. The trailing `sync` is required or you measure
the page cache:

```bash
sudo -u ditto-svc sh -c 'time { dd if=/dev/zero of=/media/ditto/speed.bin bs=1M count=50; sync; }'
sudo -u ditto-svc rm /media/ditto/speed.bin
```

Expect roughly 1 MB/s. That figure is what sizes the loop-staging timeout in
`config.LOOP_STAGE_TIMEOUT`.

---

## Checklist

- [ ] `dittobacktracker.local` resolves from your laptop
- [ ] Pedal appears at `/dev/disk/by-label/DITTOPLUS` when connected
- [ ] `systemctl show ditto-web -p User` reports `ditto-svc`
- [ ] `id ditto-svc` shows it is **not** in the `sudo` group
- [ ] `ditto-svc` can mount and unmount the pedal without becoming root
- [ ] Both scoped rules answer for `ditto-svc` (below)
- [ ] **Done** in the web UI powers the device off
- [ ] `systemctl status ditto-web` is active, with no restart loop
- [ ] The web UI loads and shows the slot grid
- [ ] A dropped MP3 converts and plays back from the pedal
- [ ] Boot to SSH in under 15 s
- [ ] `df -h /var/lib/ditto` shows `/dev/mmcblk0p3`, not an overlay
- [ ] Ten hard power cuts leave no fsck and nothing corrupt on the pedal

Check the two sudoers rules without firing either, by asking sudo whether it
would allow them. Each prints the command back if the rule matches and fails if
it does not:

```bash
sudo -u ditto-svc sudo -n -l /sbin/poweroff
sudo -u ditto-svc sudo -n -l /usr/bin/systemctl start --no-block ditto-restart.service
```

Worth doing explicitly, because both fail quietly in use. A refused poweroff
arrives after the page has already said it is safe to unplug, and a refused
restart leaves the device serving the old code after reporting an update.
**Done** is the end-to-end version of the first and a good last step.

The `df` check is the one people miss. If the data partition is overlaid,
everything works until the first reboot, then every upload is gone and nothing
reported an error.
