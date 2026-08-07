# Provisioning the Pi

Setting up a Raspberry Pi Zero 2 W to run DittoBackTracker. About an hour, most
of it waiting on `apt`.

Two things here are unusual: a **separate data partition** and a **read-only
root filesystem**. The device loses power when you flip a slide switch, so
nothing may be written to the OS card during normal operation.

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

```
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

```
LABEL=dittodata  /var/lib/ditto  ext4  defaults,noatime  0  2
```

```bash
sudo mount -a
sudo mkdir -p /var/lib/ditto/{sources,staged,trash,app}
sudo chown -R ditto:ditto /var/lib/ditto
```

This partition holds the application, the uploads and the database. It is the
only writable storage once the overlay is on.

---

## 3. Packages

```bash
sudo apt update && sudo apt full-upgrade -y
sudo apt install -y \
  git ffmpeg \
  python3-flask python3-waitress python3-smbus python3-gpiozero \
  i2c-tools avahi-daemon \
  overlayroot
```

Everything from apt, nothing from pip. A read-only root and a virtualenv are an
awkward combination.

---

## 4. I2C and GPIO access

Skip this if you aren't fitting the UPS HAT, button, LED or display.

```bash
sudo raspi-config nonint do_i2c 0
sudo usermod -aG i2c,gpio ditto
sudo reboot
```

The group membership matters. The service runs as `ditto`, and without it every
hardware probe fails with a permission error that the code treats the same as
"not fitted" — so a missing group looks exactly like missing hardware.

With the HAT fitted:

```bash
sudo i2cdetect -y 1     # expect 0x43, and 0x3c if a display is fitted
```

Note that `sudo i2cdetect` proves the bus works, not that the service can reach
it. Check as the service user too:

```bash
i2cdetect -y 1          # no sudo
```

An empty grid under `sudo` usually means the pogo pins aren't contacting the
GPIO pads. They can carry enough current to power the Pi while missing the
signal pins, so the board appears to work. Screw the boards together instead of
relying on pin pressure.

---

## 5. USB host mode

The Zero's data port defaults to host mode. Set it explicitly in
`/boot/firmware/config.txt`:

```
dtoverlay=dwc2,dr_mode=host
```

Reboot, plug in the pedal, and check:

```bash
lsusb                          # TC Electronic Ditto Plus
ls -l /dev/disk/by-label/      # DITTOPLUS
```

Udev creates the `by-label` symlink. No custom rule needed.

---

## 6. Mounting

An fstab entry with `noauto,user` lets the service mount the pedal without
root:

```
LABEL=DITTOPLUS  /media/ditto  vfat  noauto,user,rw,flush,fmask=077,dmask=077,uid=ditto,gid=ditto  0  0
```

`fmask=077,dmask=077` keeps the mounted files owner-only (the `ditto` user the
service runs as) rather than the world-writable `umask=000`.

```bash
sudo mkdir -p /media/ditto
```

As the `ditto` user, with no sudo:

```bash
mount /media/ditto
ls /media/ditto          # 01track/ … 99track/
umount /media/ditto
```

`flush` pushes FAT writes out promptly instead of leaving them in cache.

The pedal stays enumerated after `umount` and does not return to looper
operation while the cable is attached. That is expected — see
[pedal-format.md](pedal-format.md#release-behaviour). Unplug the dongle when
you're done with it.

---

## 7. Install

Clone onto the data partition. Your home directory is on the root filesystem,
which becomes read-only in step 9, so a checkout there can't be updated
afterwards.

```bash
git clone https://github.com/rlhatcher/DittoBackTracker.git /var/lib/ditto/src
cd /var/lib/ditto/src
./install.sh
```

Open `http://dittobacktracker.local/`, plug in the pedal, drop a track in.

Do this before enabling the overlay in step 9. `install.sh` writes to `/etc`,
and those changes are discarded once the root filesystem is read-only.

The installer is idempotent, and repeats the package install and fstab entry
from steps 3 and 6. It does not install `overlayroot`, so don't skip step 3.

---

## 8. Boot time

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

```
disable_splash=1
dtoverlay=disable-bt
boot_delay=0
```

Measure with `systemd-analyze` and `systemd-analyze blame | head -15`.

Do this before step 9. Each attempt needs a writable root.

---

## 9. Read-only root filesystem

Last, once everything above works.

Stop journald writing to the card:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nStorage=volatile\nRuntimeMaxUse=16M\n' \
  | sudo tee /etc/systemd/journald.conf.d/volatile.conf
```

Then set `/etc/overlayroot.conf`:

```
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
touch /var/lib/ditto/x && echo ok && rm /var/lib/ditto/x
```

### Changing anything afterwards

Application code lives on `/var/lib/ditto/app`, which stays writable, so
updating is:

```bash
cd /var/lib/ditto/src && git pull
rm -rf /var/lib/ditto/app/ditto
cp -r ditto /var/lib/ditto/app/
sudo systemctl restart ditto-web
```

The `rm` matters: without it, modules deleted upstream linger in the deployed
copy.

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

---

## 10. Optional: measure throughput and current

Neither is needed. They're useful if you want numbers for your own hardware.

Write throughput, with the pedal mounted. The trailing `sync` is required or
you measure the page cache:

```bash
time { dd if=/dev/zero of=/media/ditto/speed.bin bs=1M count=50; sync; }
rm /media/ditto/speed.bin
```

Battery current. Negative means the cell is discharging:

```bash
python3 - <<'EOF'
import smbus, time
BUS, ADDR, SHUNT_OHMS = 1, 0x43, 0.1
b = smbus.SMBus(BUS)

def read(reg):
    d = b.read_i2c_block_data(ADDR, reg, 2)
    v = (d[0] << 8) | d[1]
    return v - 65536 if v > 32767 else v

print(" bus V   shunt mV   current mA")
for _ in range(20):
    bus_v    = (read(0x02) >> 3) * 0.004      # 4 mV per LSB
    shunt_mv = read(0x01) * 0.01              # 10 uV per LSB
    print(f"{bus_v:6.3f}   {shunt_mv:8.2f}   {shunt_mv/SHUNT_OHMS:10.1f}")
    time.sleep(0.5)
EOF
```

The 0.1 Ω shunt value comes from Waveshare's FAQ for this board. A full cell
reads about 4.2 V and an empty one about 3.1 V, which is all the charge
estimate has to work from.

---

## Checklist

- [ ] `dittobacktracker.local` resolves from your laptop
- [ ] Pedal appears at `/dev/disk/by-label/DITTOPLUS` when connected
- [ ] The `ditto` user can mount and unmount without `sudo`
- [ ] `systemctl status ditto-web` is active, with no restart loop
- [ ] The web UI loads and shows the slot grid
- [ ] A dropped MP3 converts and plays back from the pedal
- [ ] Boot to SSH in under 15 s
- [ ] `df -h /var/lib/ditto` shows `/dev/mmcblk0p3`, not an overlay
- [ ] Ten hard power cuts leave no fsck and nothing corrupt on the pedal

The `df` check is the one people miss. If the data partition is overlaid,
everything works until the first reboot, then every upload is gone and nothing
reported an error.
