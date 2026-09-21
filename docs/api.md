# HTTP API

Everything the web UI does goes through this. There is no authentication.

Base URL is the device, e.g. `http://dittobacktracker.local/`.

Two things live here, and they are not the same. The **library** is
every track on the device. It survives slots being cleared, and only an
explicit delete removes one. **Slots** are what the pedal is carrying right now:
99 assignments pointing at library tracks. A slot's `display_name` and
`duration` are read from its library track, so renaming once changes it
everywhere.

---

## GET /api/state

Full snapshot. The same object is pushed over `/api/events`.

```json
{
  "seq": 4128,
  "pedal": "mounted",
  "busy": null,
  "progress": null,
  "error": null,
  "version": "0.4.0",
  "revision": "a1b2c3d",
  "update_available": false,
  "remote_revision": null,
  "capacity": {
    "used_seconds": 3.024,
    "total_seconds": 3780,
    "used_label": "0:03",
    "total_label": "63:00"
  },
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
| `seq` | Monotonically increasing snapshot counter. `/api/events` sends strictly increasing frames and drops the rest, so a consumer never sees state roll backwards; a hand-rolled consumer reading the queue itself should do the same |
| `pedal` | `absent`, `mounted`, `error` |
| `busy` | Description of current work, or `null` when idle. Unplug only when it is `null` |
| `progress` | 0.0–1.0 during conversion, else `null` |
| `slot_count` | How many slots the pedal has. Clients should use this rather than assume 99 |
| `loops` | Slot numbers that hold a pedal-recorded `LOOP.WAV`. Detected once on mount and only meaningful while `pedal` is `mounted`; a slot can appear here with no matching entry in `slots` (a loop with no backing track) |
| `version` | Package version string |
| `revision` | Short git commit of the deployed code, or `null` off a git checkout. Watch it change to confirm an update took |
| `update_available` | `true` when the tracked remote branch's commit differs from the deployed commit (the device converges to the remote, so a rebased or force-pushed branch counts too, not only a strictly newer one). Checked once at startup and again on demand via `POST /api/update/check`. There is no background polling. `false` before the first check and when up to date. A check that can't run (offline, no checkout) leaves the last known value unchanged |
| `remote_revision` | Short git commit the remote is at, when `update_available`; else `null` |

`capacity.used_seconds` is derived from the bytes on the volume while the pedal
is mounted, so it already counts `LOOP.WAV` and anything else physically
present. Unmounted, it falls back to summed backing-track durations.

Slot `state` is one of `converting`, `staged`, `synced`, `error`. Slots with no
entry are absent from the array. A slot reads as `synced` when
`source_hash == synced_hash`.

---

## POST /api/upload

Multipart. One or more `file` parts. This is what the UI uses.

A leading number in the filename picks the slot (`07 Blue Bossa.mp3` → slot
7). Files without one, or whose number is taken, fill the lowest free slots.
Slots holding a recorded `LOOP.WAV` are skipped. To put a track in a
particular slot afterwards, move it with `POST /api/slots/<n>/move`.

```bash
curl -F 'file=@07 Blue Bossa.mp3' http://dittobacktracker.local/api/upload
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

## DELETE /api/slots/&lt;n&gt;

Clear slot `n`. `BT.WAV` is removed from the pedal on the next pass. A recorded
`LOOP.WAV` in the same slot is left alone, and the track stays in the library,
so putting it back is an assign.

```json
{ "ok": true }
```

---

## GET /api/loops/&lt;n&gt;

Download the pedal-recorded loop in slot `n`, streamed straight off the pedal
as an attachment. **Download never deletes.** A failed or partial download
costs nothing; just retry. A whole-pedal loop is ~477 MiB and the USB link is
~1 MB/s, so a large one takes minutes.

```bash
curl -OJ http://dittobacktracker.local/api/loops/5    # -> loop-05.wav
```

| Status | Meaning |
|---|---|
| `200` | `Content-Disposition: attachment; filename="loop-NN.wav"`, `audio/wav` body |
| `404` | The slot has no loop |
| `503` | No pedal is mounted |

---

## DELETE /api/loops/&lt;n&gt;

Delete the loop in slot `n` from the pedal. Irreversible, because a loop is a live take
with no source and no undo. `BT.WAV` and the slot directory are never touched.

```json
{ "ok": true }
```

`404` if the slot has no loop. `409` while a job holds the pedal; retry when
it finishes.

---

## POST /api/slots/&lt;n&gt;/move

```json
{ "to": 12 }
```

Moves to an empty destination, or **swaps** with an occupied one. Swapping is
non-destructive, so reordering never loses a track. Moving to the same slot
is a no-op.

---

## GET /api/library

Every track on the device, newest first.

```json
[ { "source_hash": "b9ecf8c94d007de0a5ae", "name": "Blue Bossa",
    "duration": 311.0, "added": 1786070717.31 } ]
```

The whole list, unpaginated: a few hundred rows is a small response, and
searching and sorting are the client's business. Deliberately **not** part of
the state snapshot, which is pushed over `/api/events` several times a second
during a conversion and has to stay small. Clients should re-read this endpoint
when the event stream (re)connects, which happens on load and on each five-minute
stream rotation.

---

## POST /api/library

Add files to the library without assigning slots. Multipart, one or more `file`
parts.

`201` with per-file results, in the same shape as `POST /api/upload`, and with
the same caveat: it is a batch, so it returns `201` even when every file was
rejected. Check `errors`; don't read `201` as "everything landed". A `400` means
the whole request was unusable (no `file` part at all).

```json
{
  "added": [ { "source_hash": "b9ec…", "name": "track", "duration": 311.0,
               "added": 1786070717.31 } ],
  "errors": [ { "name": "notes.txt", "error": "not an audio file" } ]
}
```
---

## PATCH /api/library/&lt;hash&gt;

Rename a track.

```json
{ "name": "Blue Bossa" }
```

`name` is 1–200 characters after trimming. Returns the updated row.

| Status | Meaning |
|---|---|
| `400` | An empty or overlong name |
| `404` | No such track |

The new name appears in the slot list and the print view immediately, because
there is only one copy of it.

---

## DELETE /api/library/&lt;hash&gt;

Forget a track, and with it the only copy of its audio.

Refuses with `409` while any slot still holds the track, naming them, so a
client can ask before destroying something the pedal is using:

```json
{ "error": "in use", "slots": [3, 7] }
```

Add `?force` to clear those slots first. On success:

```json
{ "ok": true, "cleared": [3, 7] }
```

A `404` means there was no such track. It can still have changed the
device, so it carries `cleared` too. That happens when the library row had
already gone (a concurrent delete, say) while slots still pointed at it: those
slots are cleared regardless, and reporting an empty result would hide it.

```json
{ "error": "not found", "cleared": [3] }
```

---

## POST /api/slots/&lt;n&gt;/assign

Put a track that is already in the library into slot `n`, without uploading it
again. Body `{"hash": "..."}`. Whatever was in the slot is replaced; the track
it held stays in the library.

`201` with the slot object. `404` if the track isn't in the library, `400` if
the slot is out of range.

The pedal holds about a dozen tracks and the library holds as many as the card
does, so this is how you change what the pedal carries, not another upload.

---

## POST /api/slots/assign

Put several library tracks on the pedal in one call. Body
`{"hashes": ["b9ec…", "ff02…"]}`. The fill begins at the first slot with room.

```json
{ "start": 9, "end": 11,
  "assigned": [ { "slot": 9, "source_hash": "b9ec...", "name": "Autumn Leaves" } ],
  "skipped_loops": [ 10 ],
  "unplaced": [ { "source_hash": "ff02...", "name": "Ceora",
                  "error": "no room past slot 99" } ],
  "loops_known": true }
```

Tracks go in the order given into consecutive slots, skipping any slot that
holds a loop, the same automatic-placement rule an unnumbered upload follows.
`start` and `end` are the first and last slot **actually written**, so they
span those skips. Both are `null` when nothing was placed. Whatever was in a
slot is replaced, as with a single assign.

`201`. `400` if `hashes` is not a non-empty list of strings. `404` if any hash
is not in the library, and then nothing is
placed: a set list with a track missing from it is one problem, and the
client's list is stale.

**Read the body.** A `201` does not mean everything landed: `unplaced` names
the tracks that ran out of pedal. `loops_known: false` means the range is
provisional: with no pedal connected the device cannot know which slots hold
loops and cannot skip them.

This is one endpoint rather than N calls to `POST /api/slots/<n>/assign`: the
loop set is only correct under the lock that queues the work, the whole fill
is one locked step so a forced delete cannot interleave with it, and it
broadcasts one snapshot instead of one per track.

---

## GET /api/library/&lt;hash&gt;/audio

Stream a track's original file, so a browser can audition it before committing
a slot to it. Supports `Range`, so `<audio>` can seek; expect `206` for a range
request and `416` for an unsatisfiable one.

The URL is content-addressed, so the response is cacheable indefinitely. Nothing
on the device plays audio. This is bytes to the browser.

`.ogg`, `.opus`, `.wma` and (in older browsers) `.flac` are accepted
for upload but not playable everywhere, Safari especially. That is a browser
limitation; the file still converts and writes normally.

---

## POST /api/update

Over-the-air self-update. Pulls the tracked branch, redeploys the app, and
restarts the service. The restart runs out of process (a separate oneshot unit),
so the browser's `EventSource` drops and reconnects; watch `revision` in
the snapshot change to confirm the new code is running.

```json
{ "ok": true, "revision": "a1b2c3d" }
```

Refused while the device is busy so a restart never interrupts a write. The
device must have OTA set up (a git checkout at `/var/lib/ditto/src` and the
restart sudoers rule). See the README.

| Status | Meaning |
|---|---|
| `200` | Update deployed; the service is restarting |
| `409` | Busy (work is in flight or queued, or an update is already running). Retry when idle |
| `502` | The update failed: no network, no git checkout, new code that failed to load (rolled back), or the restart was not permitted. Body is `{"error": "..."}` |

---

## POST /api/update/check

Check the remote for a newer version, on demand. This is the manual counterpart to the
startup check, since the device does no background polling. Fetches the tracked
branch and re-derives `update_available`; if it changed, the new state is also
pushed over `/api/events`.

Always `200`. `ok` is `false` with an `error` string when the check couldn't run:

```json
{ "ok": true, "error": null, "revision": "a1b2c3d",
  "update_available": true, "remote_revision": "e5f6a7b" }
```

| Field | Meaning |
|---|---|
| `ok` | `false` when the check couldn't run. `error` says why: no deployment (not a git checkout), `couldn't reach the remote` or `couldn't read the remote branch` (offline or bad remote), or an update is already running |
| `update_available` / `remote_revision` | Same meaning as in the state snapshot |

Blocks briefly on the `git fetch` (seconds). State-changing, so the same
cross-site guard as the other write methods applies (`403` on a foreign origin).

---

## GET /api/events

Server-sent events. One `data:` frame containing a full state snapshot on
connect, then another on every change. If no change occurs for 15 s a comment
line is sent instead, so a frame of some kind always arrives within 15 s.

The stream is closed after five minutes and the browser's `EventSource`
reconnects on its own. A hand-rolled consumer should expect the stream to end
and reconnect.

Frames may be **coalesced, never reordered**. Each frame is the whole state, so
a consumer that falls behind has its backlog discarded and receives the newest
snapshot rather than a queue of stale ones. Every frame carries a `seq`, and the
stream only ever sends a frame whose `seq` is higher than the last one it sent,
including the very first, which can otherwise race frames queued while the
stream was being set up. Don't treat the stream as a change log; treat each
frame as the current truth.

```bash
curl -N http://dittobacktracker.local/api/events
```

---

## Errors

| Status | Where | Meaning |
|---|---|---|
| `400` | `POST /api/slots/<n>/move`, `/assign`, `DELETE /api/slots/<n>`, `POST /api/upload`, `POST /api/slots/assign`, `PATCH /api/library/<hash>` | Bad input: slot out of range, no files, a body without a list of hashes, or an empty/overlong name. Body is `{"error": "..."}` |
| `403` | any state-changing method (not `GET`/`HEAD`/`OPTIONS`) | Cross-site request. There is no auth, so requests carrying a foreign `Origin` or a cross-site `Sec-Fetch-Site` are refused |
| `404` | `GET`/`DELETE /api/loops/<n>`, `PATCH`/`DELETE /api/library/<hash>`, `GET /api/library/<hash>/audio`, `POST /api/slots/<n>/assign`, `POST /api/slots/assign` | The slot has no loop; or no such track in the library, which is also what a malformed hash returns, since it cannot name one |
| `409` | `POST /api/update` | Busy: work is in flight or queued, or an update is already running. Retry when idle |
| `409` | `DELETE /api/loops/<n>` | A job holds the pedal. Retry when it finishes |
| `409` | `DELETE /api/library/<hash>` | A slot still holds the track. Body carries `slots`; repeat with `?force` to clear them first |
| `416` | `GET /api/library/<hash>/audio` | The requested byte range lies outside the file |
| `413` | `POST /api/upload`, `POST /api/library` | Request body exceeds the upload size limit (512 MB by default, set with `DITTO_MAX_UPLOAD_MB`) |
| `502` | `POST /api/update` | The update failed: no network, no git checkout, new code that failed to load (rolled back), or the restart was not permitted. Body is `{"error": "..."}` |
| `503` | `GET /api/loops/<n>` | No pedal mounted. Body is `{"error": "..."}` |

`POST /api/upload` and `POST /api/library` are the exceptions to the rule. Both
are batches, so they return `201` even when some or all files were rejected,
and report those per file in `errors`. A `400` from either means the whole
request was unusable (no `file` part at all). Check `errors` on a `201`; don't
treat `201` as "everything landed".

A batch also reports per file when the device could not *store* a file, a full
card or bytes it could not confirm, rather than failing the whole request, so
the files that did land are never silently lost.

Anything else is a bug.
