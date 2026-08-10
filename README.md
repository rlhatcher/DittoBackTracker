# DittoBackTracker

Load backing tracks onto a TC Electronic Ditto+ looper over WiFi.

The Ditto+ plays backing tracks, but getting them onto it needs a computer. The
pedal mounts as a USB drive and wants 44.1 kHz / 24-bit / **mono** WAV in a
specific folder layout.

DittoBackTracker is a battery-powered dongle that sits between them. Plug it
into the pedal, open a web page, drag MP3s in. It converts them to the pedal's
format and writes them to the right slots.

```text
  Phone / laptop                    ┌──────────────────────┐
  ┌──────────────┐    your WiFi     │  DittoBackTracker    │
  │  drag & drop │ ───────────────▶ │   Pi Zero 2 W        │      mini-USB
  │   web page   │                  │   1S LiPo + boost    │ ───────────────▶ Ditto+
  └──────────────┘                  └──────────────────────┘
```

## What it does

- Drag and drop from any browser
- Converts to the pedal's format
- 99-slot map showing what is loaded, converting or written
- Drag between slots to reorder; dropping onto an occupied slot swaps them
- Drag to the bin to remove, with undo
- Print the loaded track list
- Capacity shown in minutes, because the pedal holds about 63 minutes in total
- Download or remove a loop the pedal recorded
- Never writes `LOOP.WAV`, so recorded loops are safe
- Read-only root filesystem, so cutting the Pi's power is unlikely to corrupt
  the system partition. `state.db`, `sources/`, `staged/` and `trash/` live on
  a separate writable partition and still need a clean unmount — end the
  session with the button or the web page rather than pulling the power

What isn't built yet is in [docs/roadmap.md](docs/roadmap.md).

---

## Parts

| Part                                         |
| -------------------------------------------- |
| Raspberry Pi Zero 2 W                        |
| Waveshare UPS HAT (C) + 1000 mAh 803040 LiPo |
| microSD card, 8 GB or larger                 |
| micro-USB OTG adapter                        |
| USB-A to **mini-B** cable                    |

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

### Updating over the air

The device updates itself. It checks for a new version at startup and whenever
you press **Check for update** at the bottom of the web page; when one is
available that control becomes **Update available**, and pressing it pulls the
new code and restarts.

First-time setup needs the read-only overlay off for one boot
([docs/provisioning.md](docs/provisioning.md#changing-anything-afterwards)); the
mechanism is in [docs/api.md](docs/api.md#post-apiupdate).

### Running it without hardware

The web UI runs anywhere. With no pedal attached, uploads convert and wait.

`ffmpeg` and `ffprobe` must be on your `PATH` first — without `ffprobe` every
upload is rejected as "not a readable audio file". On macOS `brew install
ffmpeg`; on Debian or Ubuntu `sudo apt install ffmpeg python3-venv` (the venv
package isn't present on a minimal install).

```bash
python3 -m venv .venv && .venv/bin/pip install flask
DITTO_DATA=/tmp/ditto-data DITTO_MOUNT=/tmp/ditto-mount \
  .venv/bin/python -m ditto --headless --host 127.0.0.1 --port 8080 --debug
```

`--headless` skips the button, LED and display. `--debug` uses Flask's built-in
server, so `waitress` isn't needed here. `--host 127.0.0.1` keeps the
unauthenticated dev server off your network; the default is `0.0.0.0`.

---

## How it works

The pedal is mounted for the length of a session and released when you press
Done. Uploads are stored by content hash, converted in the background, and
written as they become ready.

```text
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

| Module      | Does                                   |
| ----------- | -------------------------------------- |
| `config.py` | Paths and constants                    |
| `db.py`     | SQLite storage                         |
| `media.py`  | ffprobe and ffmpeg                     |
| `pedal.py`  | Detect, mount, write `BT.WAV`, unmount |
| `power.py`  | Battery gauge and charge thresholds    |
| `oled.py`   | SSD1306 display driver                 |
| `panel.py`  | Button, LED, display — each optional   |
| `core.py`   | Session lifecycle and work queue       |
| `web.py`    | Flask routes and server-sent events    |

The HTTP API is documented in [docs/api.md](docs/api.md).

---

## The pedal

[docs/pedal-format.md](docs/pedal-format.md) documents what the Ditto+ actually
expects: the audio format, the folder layout, the `BT.WAV` and `LOOP.WAV`
distinction, real capacity, and what happens when you try to make it release
over USB. None of that is in TC Electronic's documentation. If you're building
something else for this pedal, start there.

---

## Licence

MIT. See [LICENSE](LICENSE).

---

## Security

There is no authentication. The service binds `0.0.0.0:80`, so anyone on the
same network can upload, clear slots, trigger a self-update, or shut the device
down. The update only ever pulls the branch this device already tracks from its
own GitHub remote, so it fetches your code, not an attacker's — but it does let a
LAN user force a restart. It is built for a home LAN. Don't put it on a network
you don't control.

---

## Disclaimer

Not affiliated with or endorsed by TC Electronic or Music Tribe. "Ditto" and
"TC Electronic" are their trademarks, used here to describe compatibility.

This writes to your pedal's internal storage. It uses atomic writes, flushes
before unmounting, and never touches `LOOP.WAV`, but it comes with no warranty.
Back up any loops you care about before first use.
