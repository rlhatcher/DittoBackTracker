# Provisioning the Pi

End state: a Zero 2 W on WiFi at `dittobacktracker.local`, read-only root, and
the pedal mountable by an unprivileged service. The [checklist](#checklist)
verifies it.

---

## 1. Image

**Raspberry Pi OS Lite (64-bit)**.

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
Imager ejects the card when it finishes. Reinsert it and edit `cmdline.txt` on
the `bootfs` partition before the first boot. On macOS that file is at
`/Volumes/bootfs/cmdline.txt`. On Linux, mount the partition first.

Delete the `resize` token and nothing else:

```text
console=serial0,115200 console=tty1 root=PARTUUID=... rootfstype=ext4
fsck.repair=yes rootwait resize cfg80211.ieee80211_regdom=GB ds=nocloud;i=rpi-imager-...
                         ^^^^^^ delete this
```

Keep it as one line.

Boot the Pi and check:

```bash
ssh ditto@dittobacktracker.local
df -h /        # expect ~2.3G, not the full card
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

## 2. Prepare the Pi

`provision.sh` does the partitioning, the packages, USB host mode and the boot
settings. Fetch it directly rather than cloning: git is not installed yet, and
the partition the repo belongs on does not exist yet either.

```bash
curl -fsSL -o provision.sh https://raw.githubusercontent.com/rlhatcher/DittoBackTracker/main/provision.sh
sudo bash provision.sh
```

It gives the root 5 GB and the data partition everything else. The root is the
one that gets locked read-only in step 4 and then never grows, so it is sized
to what it holds: 2.6 GB after these packages, of which ffmpeg and its
dependencies are about 900 MB. The data partition takes the rest because it is
the half that fills up, and on a 32 GB card that is around 26 GB.

It is idempotent, and it will not format a partition that already holds a
filesystem, so re-running it cannot take the library with it.

```bash
sudo reboot
```

The reboot is needed: USB host mode and the boot settings only take effect from
`/boot/firmware/config.txt` at boot. Afterwards the pedal should enumerate:

```bash
lsusb                          # TC Electronic Ditto Plus
ls -l /dev/disk/by-label/      # DITTOPLUS
```

Boot is 20-40 s stock and about 10 s after this. Measure with
`systemd-analyze` and `systemd-analyze blame | head -15`.

---

## 3. Install

Both parts of this step have to happen before step 4, because your home
directory and `/etc` both become read-only there: a checkout in your home
directory could never be updated, and `install.sh` writes to `/etc`.

Clone onto the data partition, which stays writable. The clone needs `sudo`
because step 2 left `/var/lib/ditto` owned by root, and `install.sh` is what
hands it to `ditto-svc`:

```bash
sudo git clone https://github.com/rlhatcher/DittoBackTracker.git /var/lib/ditto/src
cd /var/lib/ditto/src
./install.sh
```

`install.sh` installs the packages, creates the `ditto-svc` account the service
runs as, gives it `/var/lib/ditto`, writes the fstab entry and both sudoers
rules, deploys the code and starts the unit. It is idempotent: run it again to
apply a change to any of those.

Open `http://dittobacktracker.local/`, plug in the pedal, drop a track in.

### The pedal mount entry

`install.sh` wrote this entry. `noauto,user` lets the service mount the pedal
without root:

```text
LABEL=DITTOPLUS  /media/ditto  vfat  noauto,user,rw,flush,fmask=077,dmask=077,uid=ditto-svc,gid=ditto-svc  0  0
```

`fmask=077,dmask=077` makes the mounted files owner-only, owned by `ditto-svc`
rather than by your login. `flush` pushes FAT writes out promptly instead of
leaving them in cache.

The pedal stays enumerated after `umount` and won't return to looper operation
while the cable is attached. That is expected, and unplugging is the only way
back. See [pedal-format.md](pedal-format.md#release-behaviour).

### Checking the pedal mount by hand

Optional. The `ditto-svc` account exists now, so the fstab entry above
can be tested. Stop the service first, or it is already holding the pedal:

```bash
sudo systemctl stop ditto-web
sudo -u ditto-svc mount /media/ditto
sudo -u ditto-svc ls /media/ditto      # 01track/ … 99track/
sudo -u ditto-svc umount /media/ditto
sudo systemctl start ditto-web
```

Every line runs as `ditto-svc`, including the `ls`. `dmask=077` makes the mount
`0700` owned by that account, so your login user cannot read it, and `user` in
fstab lets anyone mount but only the mounting user unmount.

---

## 4. Read-only root filesystem

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

Run the write test as `ditto-svc`. That is the account that has to write
there, and your login user cannot.

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
the `git pull` as yourself and git refuses with "detected dubious ownership".

Or press **Update** in the web UI for the same result over the air. How that
works is in [api.md](api.md#post-apiupdate).

Either way, only the `ditto/` package moves. A release that also changes the
systemd unit, the sudoers rules, the fstab entry or the ownership of
`/var/lib/ditto` needs `install.sh` run again, and `install.sh` writes to
`/etc`, so the overlay has to come off for that boot. See below.

For changes under `/etc`:

```bash
sudo overlayroot-chroot        # writes land on the real root filesystem
```

Or disable the overlay for a session. This edit has to happen inside the
chroot: editing the file normally writes to the tmpfs layer, which is then
discarded:

```bash
sudo overlayroot-chroot
sed -i 's/^overlayroot=.*/overlayroot=""/' /etc/overlayroot.conf
exit
sudo reboot
```

Re-enable it once you're done. The overlay is off now, so the root is writable
and you edit `/etc/overlayroot.conf` directly, with no chroot:

```bash
sudo sed -i 's/^overlayroot=.*/overlayroot="tmpfs:recurse=0"/' /etc/overlayroot.conf
sudo reboot
```

---

## 5. Optional: measure write throughput

Not needed. Useful if you want a number for your own card and cable.

With the pedal mounted, and as `ditto-svc`, since the volume is owner-only.
The trailing `sync` is required or you measure the page cache:

```bash
sudo -u ditto-svc sh -c 'time { dd if=/dev/zero of=/media/ditto/speed.bin bs=1M count=50; sync; }'
sudo -u ditto-svc rm /media/ditto/speed.bin
```

Expect roughly 1 MB/s. That is where `config.LOOP_STAGE_TIMEOUT` comes from.

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
Pressing **Done** tests the poweroff rule for real, so it makes a good last
step.

The `df` check is the one people miss. If the data partition is overlaid,
everything works until the first reboot, then every upload is gone and nothing
reported an error.
