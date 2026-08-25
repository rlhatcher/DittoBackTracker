# DittoBackTracker

[![CI](https://dl.circleci.com/status-badge/img/gh/rlhatcher/DittoBackTracker/tree/main.svg?style=shield)](https://dl.circleci.com/status-badge/redirect/gh/rlhatcher/DittoBackTracker/tree/main)

Load backing tracks onto a TC Electronic Ditto+ looper over WiFi.

The Ditto+ plays backing tracks but has no way to receive them. It mounts as a
USB mass storage device and expects 44.1 kHz / 24-bit / mono WAV in numbered
slot directories. DittoBackTracker is a Raspberry Pi Zero 2 W that stays
connected to the pedal, serves a web page on the local network, converts
whatever is uploaded and writes it to the pedal.

- Upload from any browser. A leading number in the filename ("07 Blue
  Bossa.mp3") assigns that slot; anything else lands in the library.
- A 99-slot map showing what is loaded, converting or written. Drag to
  reorder, swap or remove.
- A library on the device. Uploads persist, so changing what the pedal carries
  is an assignment rather than another upload.
- Folders, search, rename and in-browser preview.
- Capacity in minutes rather than slots, because the pedal holds about 63
  minutes in total.
- Loops recorded on the pedal can be downloaded or removed. `LOOP.WAV` is
  never written.

Unbuilt work is listed in [docs/roadmap.md](docs/roadmap.md).

---

## Requirements

| Part | Notes |
|---|---|
| Raspberry Pi Zero 2 W | No GPIO header needed |
| microSD card, 8 GB or larger | A1 rated |
| micro-USB OTG adapter | Pi micro-B to USB-A |
| USB-A to mini-B cable | One ships with the pedal |
| USB power supply | 5 V, 2 A |

About £39. Costs and the cable chain are in
[docs/hardware.md](docs/hardware.md).

---

## Installation

### 1. Provision the Pi

Follow [docs/provisioning.md](docs/provisioning.md). Allow about an hour, most
of it waiting on `apt`.

This step is required, not a recommendation. The device has no battery and
loses power the moment the plug comes out, so it needs a read-only root
filesystem and a separate writable data partition. Installing onto a stock
image will corrupt the card.

### 2. Install

Clone onto the data partition. Provisioning makes the home directory read-only,
so a checkout there cannot be updated afterwards.

```bash
git clone https://github.com/rlhatcher/DittoBackTracker.git /var/lib/ditto/src
cd /var/lib/ditto/src
./install.sh
```

`install.sh` installs the packages, creates the `ditto-svc` service account and
gives it `/var/lib/ditto`, writes the pedal's fstab entry and both sudoers
rules, deploys the code and starts the unit. It is idempotent, and re-running
it is how changes to any of those are applied.

It must run before the read-only overlay is enabled, because it writes to
`/etc`.

### 3. Verify

Open `http://dittobacktracker.local/` and connect the pedal.

Work through the [checklist](docs/provisioning.md#checklist) before calling it
done. It covers the two privileged operations that fail silently: a refused
poweroff arrives after the page has said it is safe to unplug, and a refused
restart leaves the device serving old code after reporting an update.

---

## Configuration

Port, pedal volume label, data and mount paths, tracked branch and upload cap
are environment variables read at startup and set in the systemd unit. Listed
in [docs/hardware.md](docs/hardware.md#settings).

---

## Updating

The device checks its tracked branch for a new commit at startup and when
**Check for update** is pressed. When one exists that control becomes **Update
available**; pressing it deploys the new code and restarts. The mechanism is in
[docs/api.md](docs/api.md#post-apiupdate).

An update replaces the `ditto/` package and nothing else. A release that also
changes the systemd unit, the sudoers rules, the fstab entry or the ownership
of `/var/lib/ditto` needs `install.sh` run again with the overlay off
([docs/provisioning.md](docs/provisioning.md#changing-anything-afterwards)).

---

## Running without hardware

The web UI runs on any machine. With no pedal attached, uploads convert and
wait.

`ffmpeg` and `ffprobe` must be on `PATH` first, or every upload is rejected as
"not a readable audio file". On macOS, `brew install ffmpeg`. On Debian or
Ubuntu, `sudo apt install ffmpeg python3-venv`.

```bash
python3 -m venv .venv && .venv/bin/pip install flask
DITTO_DATA=/tmp/ditto-data DITTO_MOUNT=/tmp/ditto-mount \
  .venv/bin/python -m ditto --host 127.0.0.1 --port 8080 --debug
```

`--debug` uses Flask's built-in server, so `waitress` is not required.
`--host 127.0.0.1` keeps the unauthenticated development server off the
network; the default is `0.0.0.0`.

---

## How it works

The pedal is mounted for the length of a session and released on **Done**.
Uploads are stored by content hash, converted in the background and written as
they become ready.

```text
  POST /api/upload
        ▼
  sources/<hash>.<ext>                     the upload, unmodified
        ▼  ffmpeg
  staged/<hash>-<codec>-<rate>-<ch>.wav    cache, keyed on target format
        ▼
  <slot>track/BT.WAV                       on the pedal
```

`sources/` is durable and `staged/` is a cache. Clearing a slot ends an
assignment and nothing more, so moving a track between slots is a database
change plus one file copy rather than another conversion. Module layering is in
[CONTRIBUTING.md](CONTRIBUTING.md#layering).

---

## Security

There is no authentication. The service binds `0.0.0.0:80`, so anyone on the
network can upload, clear slots, download a recorded loop, trigger a
self-update or shut the device down. The update only pulls the branch the
device already tracks from its own remote, so it fetches your code rather than
an attacker's, but a LAN user can still force a restart. Built for a home LAN.
Do not put it on a network you do not control.

The service runs as `ditto-svc`, a system account with no shell that is not in
the `sudo` group. `etc/99-ditto-poweroff` and `etc/99-ditto-restart` are scoped
to one command each and are the whole of what the service may do as root: power
the device off, and start the restart helper. A LAN user reaching
`POST /api/update` can restart the device onto code from the tracked branch and
can shut it down. They cannot get root.

That holds only because the account is not the login account. The Raspberry Pi
Imager user is in the `sudo` group, so a service running as it would put root
behind the same reach and reduce the rules in `etc/` to a statement of intent.

---

## Documentation

| | |
|---|---|
| [provisioning.md](docs/provisioning.md) | Setting up the Pi, start to finish |
| [hardware.md](docs/hardware.md) | Parts, power, settings |
| [api.md](docs/api.md) | HTTP API |
| [pedal-format.md](docs/pedal-format.md) | What the Ditto+ expects, measured. Not in TC Electronic's docs |
| [loop-processing.md](docs/loop-processing.md) | Reading loops off the pedal |
| [roadmap.md](docs/roadmap.md) | Unbuilt and not planned |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Layering, tests, commit conventions |

---

## Licence

MIT. See [LICENSE](LICENSE).

---

## Disclaimer

Not affiliated with or endorsed by TC Electronic or Music Tribe. "Ditto" and
"TC Electronic" are their trademarks, used here to describe compatibility.

This writes to the pedal's internal storage. It uses atomic writes, flushes
before unmounting and never touches `LOOP.WAV`, but comes with no warranty.
Back up any loops you care about before first use.
