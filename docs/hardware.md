# Hardware

## Parts

| Part | Notes | ~Cost |
|---|---|---|
| Raspberry Pi Zero 2 W | 65×30 mm, WiFi, USB OTG host | £18 |
| microSD card, 8 GB or larger | A1 rated | £6 |
| micro-USB OTG adapter | Pi micro-B to USB-A | £3 |
| USB-A to **mini-B** cable | The Ditto+ port is mini-B. One ships with the pedal | £4 |
| USB power supply | 5 V, 2 A into the Pi's PWR port | £8 |
| | | **£39** |

Cable chain: Pi micro-USB-B (data) → OTG adapter → USB-A → mini-B → Ditto+.
The Pi's other micro-USB port (PWR) takes the supply.

The pedal keeps its own 9 V supply and doesn't draw meaningfully from USB.

The web UI is the only interface.

---

## Power

The device runs from mains through the Pi's PWR port. There is no battery, no
UPS and no off switch. Pull the plug when the status line at the bottom of the
page reads "Ready", and not while it names a write in progress.

That is safe because the root filesystem is read-only, the database and every
file on the data partition are written to a temp name, fsynced and renamed,
and `BT.WAV` is written the same way with the pedal mounted `flush`. A cut
mid-write leaves a `~bt*.tmp` on the pedal, cleaned on the next mount, and
anything half-done on the data partition is swept at the next start.

---

## Settings

All are environment variables read at startup.

| Variable | Default | Purpose |
|---|---|---|
| `DITTO_PORT` | `80` | HTTP port |
| `DITTO_PEDAL_LABEL` | `DITTOPLUS` | Volume label used to find the pedal |
| `DITTO_DATA` | `/var/lib/ditto` | Data directory |
| `DITTO_MOUNT` | `/media/ditto` | Pedal mount point |
| `DITTO_UPDATE_BRANCH` | `main` | Branch the device tracks for updates |
| `DITTO_MAX_UPLOAD_MB` | `512` | Whole-request upload cap |

Set them in the systemd unit, which lives on the read-only filesystem. See
[provisioning.md](provisioning.md#changing-anything-afterwards):

```ini
[Service]
Environment=DITTO_PORT=8080
```
