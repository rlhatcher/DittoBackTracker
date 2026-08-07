# HTTP API

Everything the web UI does goes through this. There is no authentication.

Base URL is the device, e.g. `http://dittobacktracker.local/`.

---

## GET /api/state

Full snapshot. The same object is pushed over `/api/events`.

```json
{
  "pedal": "mounted",
  "busy": null,
  "progress": null,
  "ending": false,
  "error": null,
  "ip": "192.168.1.42",
  "format": { "codec": "pcm_s24le", "sample_rate": 44100, "channels": 1 },
  "format_source": "probed from BT.WAV",
  "capacity": {
    "bytes_free": 496000000,
    "bytes_total": 500170752,
    "rate": 132300,
    "used_seconds": 3.024,
    "total_seconds": 3780,
    "used_label": "0:03",
    "total_label": "63:00"
  },
  "battery": {
    "available": false,
    "percent": null,
    "charging": null,
    "can_write": true
  },
  "panel": { "led": false, "button": false, "oled": false },
  "slots": [
    {
      "slot": 5,
      "display_name": "05 Track",
      "duration": 3.024,
      "state": "synced",
      "source_hash": "b9ecf8c94d007de0a5ae",
      "synced_hash": "b9ecf8c94d007de0a5ae",
      "error": null,
      "updated": 1786070717.31
    }
  ]
}
```

| Field | Values |
|---|---|
| `pedal` | `absent`, `mounted`, `error` |
| `busy` | Description of current work, or `null` when idle |
| `progress` | 0.0–1.0 during conversion, else `null` |
| `format_source` | Which pedal file the target format was read from |
| `battery.available` | `false` when no gauge is fitted; the other battery fields are then `null` |
| `battery.can_write` | `false` blocks writes. True when charging or when no gauge is present |
| `panel` | Empty object when running `--headless` |

Slot `state` is one of `converting`, `staged`, `synced`, `error`. Slots with no
entry are absent from the array. `synced` is derived from
`source_hash == synced_hash`, not stored.

---

## POST /api/upload

Multipart. One or more `file` parts. This is what the UI uses.

| Field | Effect |
|---|---|
| `file` | Repeatable |
| `start` | Optional, 1–99. Files fill consecutive slots from here |

Without `start`, a leading number in the filename picks the slot
(`07 Blue Bossa.mp3` → slot 7). Files without one, or whose number is taken,
fill the lowest free slots. Slots holding a recorded `LOOP.WAV` are skipped
during automatic assignment, though they can be targeted explicitly.

With `start`, filename numbers are ignored — an explicit target wins. Files that
would land past slot 99 are reported as errors rather than placed elsewhere.

```bash
curl -F 'file=@track.mp3' -F 'start=7' http://dittobacktracker.local/api/upload
```

`201` with per-file results:

```json
{
  "added": [ { "slot": 7, "display_name": "track", "state": "converting", "...": "" } ],
  "errors": [ { "name": "notes.txt", "error": "not an audio file" } ]
}
```

Partial success is normal: `added` and `errors` can both be non-empty.

---

## POST /api/slots/&lt;n&gt;

Upload a single file to slot `n`. Multipart, one `file` part. Replaces whatever
is there, moving the previous entry to the trash.

`201` with the slot object, or `400` with `{"error": "..."}`.

---

## DELETE /api/slots/&lt;n&gt;

Clear slot `n`. The entry moves to the trash and `BT.WAV` is removed from the
pedal on the next pass. A recorded `LOOP.WAV` in the same slot is left alone.

---

## POST /api/slots/&lt;n&gt;/move

```json
{ "to": 12 }
```

Moves to an empty destination, or **swaps** with an occupied one. Swapping is
non-destructive, so reordering never touches the trash. Moving to the same slot
is a no-op.

---

## POST /api/slots/&lt;n&gt;/retry

Re-run a failed conversion. Ignored if the slot is empty.

---

## GET /api/trash

The 50 most recent deletions, newest first.

```json
[ { "id": 3, "slot": 12, "display_name": "Blue Bossa",
    "duration": 311.0, "deleted": 1786070717.31 } ]
```

Entries are kept 30 days.

---

## POST /api/trash/&lt;id&gt;/restore

Put a deleted entry back in its original slot, reconverting if needed. Returns
`{"slot": n}`, or `404` if the entry has gone.

---

## POST /api/session/end

Finish queued writes, flush, unmount the pedal, then power off. Same as the
physical button. Returns immediately; watch `/api/events` for progress.

---

## GET /api/events

Server-sent events. One `data:` frame containing a full state snapshot on
connect, then another on every change. A comment line is sent every 15 s to keep
the connection open.

```bash
curl -N http://dittobacktracker.local/api/events
```

---

## Errors

`400` with `{"error": "..."}` for bad input — unknown slot number, a
non-audio file, or a file ffprobe can't read. `404` for a missing trash entry.
Anything else is a bug.
