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
  "version": "0.3.0",
  "revision": "a1b2c3d",
  "update_available": false,
  "remote_revision": null,
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
  "slot_count": 99,
  "loops": [ 5, 12 ],
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
| `slot_count` | How many slots the pedal has. Clients should use this rather than assume 99 |
| `loops` | Slot numbers that hold a pedal-recorded `LOOP.WAV`. Detected once on mount and only meaningful while `pedal` is `mounted`; a slot can appear here with no matching entry in `slots` (a loop with no backing track) |
| `version` | Package version string |
| `revision` | Short git commit of the deployed code, or `null` off a git checkout. Watch it change to confirm an update took |
| `update_available` | `true` when a startup check found the tracked remote branch ahead of the deployed commit. `false` until the check completes, if it's up to date, or if the check couldn't run (offline, no checkout) |
| `remote_revision` | Short git commit the remote is at, when `update_available`; else `null` |

`capacity.used_seconds` is derived from the real bytes on the volume
(`(bytes_total − bytes_free) / rate`) while the pedal is mounted, so it already
counts `LOOP.WAV` and anything else physically present. Unmounted, it falls back
to summed backing-track durations.

Slot `state` is one of `converting`, `staged`, `synced`, `error`. Slots with no
entry are absent from the array. The database does record a `state` (and
`db.mark_synced` writes `synced`), but the API always re-derives whether a slot
is `synced` from `source_hash == synced_hash` rather than trusting the stored
flag.

---

## POST /api/upload

Multipart. One or more `file` parts. This is what the UI uses.

| Field | Effect |
|---|---|
| `file` | Repeatable |
| `start` | Optional, 1–`slot_count` (99 today). Files fill consecutive slots from here |

Without `start`, a leading number in the filename picks the slot
(`07 Blue Bossa.mp3` → slot 7). Files without one, or whose number is taken,
fill the lowest free slots. Slots holding a recorded `LOOP.WAV` are skipped
during automatic assignment, though they can be targeted explicitly.

With `start`, filename numbers are ignored — an explicit target wins. Files that
would land past the last slot (`slot_count`) are reported as errors rather than
placed elsewhere.

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

```json
{ "ok": true, "trash_id": 7 }
```

`trash_id` names the entry this call created, for a subsequent restore. It is
`null` if the slot was already empty.

---

## GET /api/loops/&lt;n&gt;

Download the pedal-recorded loop in slot `n`. The worker copies `LOOP.WAV` off
the pedal to a transient staging file on the Pi, the response streams that file
as an attachment, and the staged copy is purged once the response completes.
**Download never deletes** — the pedal keeps the original, so a failed or partial
download costs nothing; just retry.

```bash
curl -OJ http://dittobacktracker.local/api/loops/5    # -> loop-05.wav
```

The request blocks briefly while staging (the copy is seconds to a minute over
the ~1 MB/s USB link). A `GET` is used deliberately: reading a loop changes
nothing on the pedal, so it needs no cross-site guard.

| Status | Meaning |
|---|---|
| `200` | `Content-Disposition: attachment; filename="loop-NN.wav"`, `audio/wav` body |
| `404` | The slot has no loop |
| `500` | Staging failed for another reason (e.g. a local I/O error copying off the pedal). Body is `{"error": "..."}` |
| `503` | No pedal is mounted, or staging exceeded its time limit |

---

## DELETE /api/loops/&lt;n&gt;

Delete the loop in slot `n` from the pedal. State-changing, so it is covered by
the same cross-site guard as the other write methods. This is irreversible —
a loop is a live take with no source and no undo — so the UI confirms first.
`BT.WAV` and the slot directory are never touched.

```json
{ "ok": true }
```

`404` if the slot has no loop.

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

## POST /api/update

Over-the-air self-update. Pulls the tracked branch, redeploys the app, and
restarts the service. The restart runs out of process (a separate oneshot unit),
so the browser's `EventSource` simply drops and reconnects; watch `revision` in
the snapshot change to confirm the new code is running.

```json
{ "ok": true, "revision": "a1b2c3d" }
```

Refused while the device is busy so a restart never interrupts a write. The
device must have OTA set up (a git checkout at `/var/lib/ditto/src` and the
restart sudoers rule) — see the README.

| Status | Meaning |
|---|---|
| `200` | Update deployed; the service is restarting |
| `409` | Busy (a job is in flight, or an update is already running) — retry when idle |
| `502` | The update failed: no network, no git checkout, or the restart was not permitted. Body is `{"error": "..."}` |

---

## GET /api/events

Server-sent events. One `data:` frame containing a full state snapshot on
connect, then another on every change. If no change occurs for 15 s a comment
line is sent instead, so a frame of some kind always arrives within 15 s.

The stream is closed after five minutes and the browser's `EventSource`
reconnects on its own. Each stream holds a server thread, so they are retired
rather than left open indefinitely — a client that reconnects needs no special
handling, but a hand-rolled consumer should expect the stream to end.

```bash
curl -N http://dittobacktracker.local/api/events
```

---

## Errors

| Status | Where | Meaning |
|---|---|---|
| `400` | `POST /api/slots/<n>`, `/move`, `/retry`, `DELETE /api/slots/<n>`, `POST /api/upload` | Bad input: slot out of range, non-audio file, or a file ffprobe can't read. Body is `{"error": "..."}` |
| `403` | any state-changing method (not `GET`/`HEAD`/`OPTIONS`) | Cross-site request. There is no auth, so requests carrying a foreign `Origin` or a cross-site `Sec-Fetch-Site` are refused |
| `404` | `POST /api/trash/<id>/restore`, `GET`/`DELETE /api/loops/<n>` | No such trash entry, or the slot has no loop |
| `409` | `POST /api/update` | Busy: a job is in flight, or an update is already running. Retry when idle |
| `413` | `POST /api/slots/<n>`, `POST /api/upload` | Request body exceeds the upload size limit (512 MB by default, set with `DITTO_MAX_UPLOAD_MB`) |
| `500` | `GET /api/loops/<n>` | Staging the loop failed unexpectedly (e.g. a local I/O error). Body is `{"error": "..."}` |
| `502` | `POST /api/update` | The update failed: no network, no git checkout, or the restart was not permitted. Body is `{"error": "..."}` |
| `503` | `GET /api/loops/<n>` | No pedal mounted, or staging the loop exceeded its time limit. Body is `{"error": "..."}` |

`POST /api/upload` is the exception to the rule. It is a batch, so it returns
`201` even when some files were rejected, and reports those per file in
`errors` — a `400` from it means the whole request was unusable (no `file`
part at all, or a `start` outside 1–`slot_count`). Check `errors` on a `201`;
don't treat `201` as "everything landed".

Anything else is a bug.
