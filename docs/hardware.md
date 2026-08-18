# Hardware

## Parts

| Part | Notes | ~Cost |
|---|---|---|
| Raspberry Pi Zero 2 W | 65×30 mm, WiFi, USB OTG host. No header needed | £18 |
| microSD card, 8 GB or larger | A1 rated | £6 |
| micro-USB OTG adapter | Pi micro-B to USB-A | £3 |
| USB-A to **mini-B** cable | The Ditto+ port is mini-B. One ships with the pedal | £4 |
| USB power supply | 5 V, 2 A into the Pi's PWR port | £8 |
| | | **£39** |

Cable chain: Pi micro-USB-B (data) → OTG adapter → USB-A → mini-B → Ditto+.
The Pi's other micro-USB port (PWR) takes the supply.

The pedal keeps its own 9 V supply and doesn't draw meaningfully from USB.

A single board, and nothing on the GPIO header: no HAT, no button, no LED, no
display. The web UI is the only interface.

---

## Power

The device runs from mains through the Pi's PWR port. There is no battery and no
UPS, so **pulling the power is an abrupt power cut** — treat it as one.

### Ending a session

Use **Done** in the web UI. It finishes any queued writes, flushes, unmounts the
pedal and halts the Pi. The page tells you when it is safe to pull the plug; it
will disconnect as the Pi goes down.

### What an abrupt cut can and can't hurt

| | Protection |
|---|---|
| The Pi's root filesystem | Read-only, so nothing to corrupt — see [provisioning.md](provisioning.md) |
| The data partition (`/var/lib/ditto`) | SQLite in WAL mode with `synchronous=FULL`; every file that matters is written to a temp name, fsynced, then renamed |
| Uploaded audio in `sources/` | fsynced on ingest before the upload is acknowledged. If that fsync fails the upload fails too, rather than reporting success for bytes that were never confirmed on the card |
| **The pedal's FAT volume** | **The exposed one.** `BT.WAV` is written temp-then-rename with an fsync and the volume is mounted `flush`, so a cut leaves either the old file or the new one — but a cut *during* a write can still leave a `~bt*.tmp` behind (cleaned automatically on the next mount) and FAT has no journal |

So: the failure mode to avoid is unplugging while the UI says it is writing. The
status line at the bottom of the page turns amber and reads "— don't unplug"
whenever the pedal is being written to. That message is the only warning there is
now that the panel LED is gone.

Anything left behind by a cut is swept on the next start, because the clean-up
that used to run only at session end now also runs at boot: interrupted
transcodes (`staged/*.wav.part`), stranded upload temporaries (`tmp*` in the
data directory, once they're an hour old so an upload in flight is never
reaped), and orphaned staged and source files. Interrupted pedal writes
(`~bt*.tmp`) are cleaned on the next mount instead, since they live on the
pedal.

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

Set them in the systemd unit, which lives on the read-only filesystem — see
[provisioning.md](provisioning.md#changing-anything-afterwards):

```ini
[Service]
Environment=DITTO_PORT=8080
```

---

## Enclosure

There isn't one. A bare Zero 2 W is 65 × 30 mm and about 5 mm tall. Both
micro-USB ports are on the same long edge and both need to stay reachable, and
the OTG adapter on the data port is bulky enough that a short pigtail may fit
better than a rigid adapter.
