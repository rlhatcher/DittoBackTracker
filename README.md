# DittoBackTracker

Load backing tracks onto a TC Electronic Ditto+ looper from your phone.

The Ditto+ plays backing tracks, but getting them onto it needs a computer. The
pedal mounts as a USB drive and wants 44.1 kHz / 24-bit / **mono** WAV in a
specific folder layout. A phone can't mount USB storage or transcode audio.

DittoBackTracker is a battery-powered dongle that sits between them. Plug it
into the pedal, open a web page, drag MP3s in. It converts them to the pedal's
format and writes them to the right slots.

```
  Phone / laptop                    ┌──────────────────────┐
  ┌──────────────┐    your WiFi     │  DittoBackTracker    │
  │  drag & drop │ ───────────────▶ │   Pi Zero 2 W        │      mini-USB
  │   web page   │                  │   1S LiPo + boost    │ ───────────────▶ Ditto+
  └──────────────┘                  └──────────────────────┘
```

Sending the MP3 rather than the WAV is what makes this practical over WiFi. A
five-minute track is about 5 MB compressed and 38 MB in the pedal's format. The
small file goes over the air; the Pi does the expansion locally.

---

## What it does

- Drag and drop from any browser
- Converts to the pedal's format, read from the pedal itself rather than hardcoded
- 99-slot map showing what is loaded, converting or written
- Drag between slots to reorder; dropping onto an occupied slot swaps them
- Drag to the bin to remove, with undo
- Capacity shown in minutes, because the pedal holds about 63 minutes in total
- Never writes `LOOP.WAV`, so recorded loops are safe
- Read-only root filesystem, so cutting the Pi's power doesn't corrupt its card

---

## Status

Working, and in use on a Pi Zero 2 W with a real Ditto+.

The button, LED and display are implemented but have not been run against real
hardware. There is no enclosure design. See
[docs/roadmap.md](docs/roadmap.md).

---

## Parts

About £61 from scratch, plus £10 if you fit the optional button, LED and display.

| Part | Notes |
|---|---|
| Raspberry Pi Zero 2 W | A soldered header is only needed for the optional display |
| Waveshare UPS HAT (C) + 1000 mAh 803040 LiPo | Same 65×30 mm footprint, pogo-pin mount |
| microSD card, 8 GB or larger | |
| micro-USB OTG adapter | |
| USB-A to **mini-B** cable | The Ditto+ port is mini-B, not micro or C |

Optional: momentary button, LED, and a 128×32 PiOLED.

Details in [docs/hardware.md](docs/hardware.md).

---

## Install

Set the Pi up first. It needs a separate data partition and a read-only root
filesystem, which is covered in [docs/provisioning.md](docs/provisioning.md).

Clone onto the data partition — your home directory becomes read-only.

```bash
git clone https://github.com/rlhatcher/DittoBackTracker.git /var/lib/ditto/src
cd /var/lib/ditto/src
./install.sh
```

Open `http://dittobacktracker.local/` and plug the pedal in.

### Running it without hardware

The web UI runs anywhere. With no pedal attached, uploads convert and wait.

```bash
python3 -m venv .venv && .venv/bin/pip install flask
DITTO_DATA=/tmp/ditto-data DITTO_MOUNT=/tmp/ditto-mount \
  .venv/bin/python -m ditto --headless --port 8080 --debug
```

`--headless` skips the button, LED and display. `--debug` uses Flask's built-in
server, so `waitress` isn't needed here.

Needs `ffmpeg` and `ffprobe` on your `PATH`. Without `ffprobe`, uploads are
rejected as "not a readable audio file".

---

## How it works

The pedal is mounted for the length of a session and released when you press
Done. Uploads are stored by content hash, converted in the background, and
written as they become ready.

```
  POST /api/upload
        ▼
  sources/<hash>.<ext>              the upload, unmodified
        ▼  ffmpeg
  staged/<hash>-<codec>-<rate>-<ch>.wav    cache, keyed on target format
        ▼
  <slot>track/BT.WAV                on the pedal
```

The format tag in the staged filename means a change of target format
invalidates the cache instead of silently reusing a wrong-format file.

Content addressing means moving a track between slots is a database change and
one file copy, with no re-conversion.

| Module | Does |
|---|---|
| `config.py` | Paths and constants |
| `db.py` | SQLite. Sync state is derived from hashes, not a stored flag |
| `media.py` | ffprobe and ffmpeg |
| `pedal.py` | Detect, mount, write `BT.WAV`, unmount |
| `power.py` | Battery gauge and charge thresholds |
| `oled.py` | SSD1306 driver, about 150 lines, no Pillow or luma |
| `panel.py` | Button, LED, display — each optional |
| `core.py` | Session lifecycle and work queue |
| `web.py` | Flask routes and server-sent events |

The HTTP API is documented in [docs/api.md](docs/api.md).

---

## The pedal

[docs/pedal-format.md](docs/pedal-format.md) documents what the Ditto+ actually
expects: the audio format, the folder layout, the `BT.WAV` and `LOOP.WAV`
distinction, real capacity, and what happens when you try to make it release
over USB. None of that is in TC Electronic's documentation. If you're building
something else for this pedal, start there.

---

## Contributing

Issues and pull requests welcome. [docs/roadmap.md](docs/roadmap.md) lists what
isn't built yet.

If you change how files are written: only `BT.WAV` is ever created or removed,
writes go to a temporary file and are renamed into place, and `os.sync()` runs
before unmounting. Breaking any of those risks the pedal's filesystem or
someone's recording.

---

## Licence

MIT. See [LICENSE](LICENSE).

---

## Security

There is no authentication. The service binds `0.0.0.0:80`, so anyone on the
same network can upload, clear slots, or shut the device down. It is built for a
home LAN. Don't put it on a network you don't control.

---

## Disclaimer

Not affiliated with or endorsed by TC Electronic or Music Tribe. "Ditto" and
"TC Electronic" are their trademarks, used here to describe compatibility.

This writes to your pedal's internal storage. It uses atomic writes, flushes
before unmounting, and never touches `LOOP.WAV`, but it comes with no warranty.
Back up any loops you care about before first use.
