# Loop processing — design notes

Why reading and deleting `LOOP.WAV` recordings works the way it does. The feature
is built; the endpoints are in [api.md](api.md#get-apiloopsn). What follows is the
reasoning behind the non-obvious choices, and the wrong turns they close off.

## Scope

- **In:** show which slots hold a pedal-recorded loop, download a loop to the
  user's device, delete a loop from the pedal.
- **Out (deferred):** a persistent on-Pi loop archive / verified "backup",
  moving a loop when its backing track moves, a staged-copy management screen.
  See [Deferred](#deferred).

## Constraints that shaped this

- **`BT.WAV` is one-directional (host → pedal) and re-derivable** — it has a
  source in `sources/` and a `source_hash`, so a lost or corrupt `BT.WAV` is
  just re-converted. **`LOOP.WAV` is the opposite:** a live take, pedal → host,
  with **no source and no way to reconstruct it.** Every decision below follows
  from that asymmetry.
- The pedal creates exactly two files, `BT.WAV` and `LOOP.WAV`
  ([pedal-format.md](pedal-format.md)). The volume may also hold our own transient
  `~bt*.tmp` and, if a Mac ever mounted it, AppleDouble junk — so we act on an
  exact-name allowlist, never on "whatever is in the directory".
- Single user, always. The device is a personal dongle with one operator, so
  contention is rare and never adversarial. That lets the design stay simple:
  FIFO, blocking reads.
- The pedal's ~1 MB/s USB and ~477 MiB capacity ([pedal-format.md](pedal-format.md))
  bound a loop at seconds-to-a-minute to copy, and every loop on the pedal at
  ≤ 477 MiB in total.

## Decisions

### Mount and session — unchanged
One **read-write** session for the whole plug-in period. BT writes and loop
reads/deletes coexist; there is no separate read-only mode, because there is no
single-purpose session in real use. Accepted residual risk: an unclean unplug
can only damage the operation *in flight* (already fronted by the
"WRITING – DON'T PULL" panel state); unplugging during reads/idle is low-risk
because every write is followed by `os.sync()`.

### Everything that touches the pedal goes through the work queue
Loop staging and loop deletion are **jobs on the existing single worker**
(`Service._work`), exactly like `convert`/`write`/`erase`. This inherits the
hardened lifecycle for free — `_in_flight` + `_drain` + `_stop`-before-unmount
already guarantee a session never unmounts mid-job. **No web-request thread ever
reads or writes the pedal directly**, which would reintroduce the
unmount-during-operation race for reads.

Ordering is plain FIFO, no priority: with a single user the queue is usually
empty.

### Download = stage to Pi, serve from Pi, purge (transient)
1. Worker copies `LOOP.WAV` from the pedal to a **temporary file on the data
   partition** (`loops/`), using the same temp-write + `fsync` + rename
   discipline as `write_track`.
2. The web thread streams that staged file to the browser.
3. The staged file is **deleted as soon as the response completes.**

The staged copy is **pure transient cache**, not storage: the pedal still holds
the original (download never deletes), so purging it loses nothing. This is the
state-based cleanup rule — *the Pi copy is disposable as long as the pedal has
the loop* — and it means **nothing accumulates on the Pi** and there is no
time-based prune to be brittle about. Staging (rather than streaming the pedal
straight to the browser) is what keeps the pedal mount held only for the bounded
copy, never for a slow or stalled phone transfer.

### Delete = a separate, explicit, per-loop action
- **Download never deletes.** Removing a loop is a distinct action the user
  takes deliberately.
- Guarded by a **calm, honest confirm** — e.g. *"Delete this loop from the
  pedal? You can't undo this."* — which guards the accidental click. The user
  owns the rest; "I just want it gone" is a legitimate intent.
- Because download never deletes and delete is separate, **a failed or partial
  download can never cost a loop.** This is the whole reason the earlier
  "download completion is unverifiable" problem does not bite: completion is not
  wired to deletion.
- Implemented as a worker job that `unlink`s **`LOOP.WAV` by exact name** in the
  slot's directory, then `os.sync()`. Never removes the slot directory, never
  touches `BT.WAV`, never recurses.

### Capacity gauge — count real bytes
Displayed **used** switches from summed DB durations to the filesystem's own
figure:

```
used_secs = (total - free) / rate      # when total and rate
```

`(total - free)` already includes `LOOP.WAV` (and anything else physically on
the volume), so the gauge stops overstating free time the moment loops exist.
No per-loop probing. Slight over-count from FAT cluster slack errs toward
showing *less* free — the safe direction.

## Data model / identification

Loops are **pedal-only**; they are not in `state.db`. A loop is identified by
its **physical slot directory** (`/NNtrack/LOOP.WAV`), independent of the DB
`slots` table that tracks backing tracks.

Loop presence is therefore only knowable while the pedal is mounted. Detect it
**once on mount** (alongside format detection / requeue) and cache the set of
loop-bearing slots; refresh after a loop delete. **Do not `stat` 99 directories
on every snapshot** — `_emit` runs often (SSE), and 99 stats over 1 MB/s USB per
frame would be visibly slow.

## Operations as jobs

| Job | Trigger | Worker action | Panel/busy label |
|---|---|---|---|
| `stage_loop` | `GET /api/loops/<slot>` | copy pedal `LOOP.WAV` → temp in `loops/`, verify readable, signal the waiting request with the path | "Reading loop" (not "WRITING") |
| `delete_loop` | `DELETE /api/loops/<slot>` | `unlink` exact `LOOP.WAV`, `os.sync()`, refresh loop cache | "Removing loop" |

The web↔worker bridge for `stage_loop` is a **blocking** one: the handler
enqueues a job carrying a completion `Event` + result/error slots, waits on it
(bounded timeout), then streams the staged file and purges it in a `finally`.
Blocking one of the waitress threads is free on a single-user box. A distinct
busy label is needed so the OLED does not say "WRITING – DON'T PULL" during a
read.

## API

Both endpoints and the snapshot's `loops` array are documented in
[api.md](api.md#get-apiloopsn). The design points behind them: a loop download is
a plain `GET` because it changes nothing on the pedal, and the snapshot carries
the loop-bearing slots directly rather than running a staging state machine,
because the download is synchronous.

## UI

- Per slot: a **loop indicator** when `has_loop`, visually distinct from the BT
  state (incoming vs outgoing).
- A **Download** affordance on a loop-bearing slot → hits `GET /api/loops/<slot>`
  → browser download. "Leave behind" is implicit — download changes nothing on
  the pedal.
- A **Remove loop** affordance → confirm dialog → `DELETE /api/loops/<slot>`.
- Naming throughout is **"Loops" / "Download loops"**, never "backup": the tool
  can delete an irreplaceable take, so it has not earned that word.

## Cleanliness invariant (v1)

> The tool creates only `BT.WAV` (and its `~bt*.tmp`). It removes only
> `BT.WAV`, and `LOOP.WAV` **only on an explicit user action**. It matches file
> names exactly, never recurses into or deletes slot directories, and leaves any
> file it does not recognise strictly alone. Pi-side staged copies are transient
> and purged after each download.

## Failure modes & accepted risks

- **Unplug mid-write/mid-delete:** can corrupt only that operation's file;
  fronted by the panel warning. Reads/idle unplug is low-risk. Accepted.
- **Failed/partial browser download:** costs nothing — the pedal keeps the loop,
  the staged copy is purged, the user retries. Deletion is unrelated.
- **Loop vanishes between `has_loop` and staging** (e.g. deleted on the pedal):
  the job surfaces an error like a failed convert; no crash, no bad delete.

## Deferred

1. **Staged-copy management surface** — not needed while staging is transient.
   Only returns if a persistent on-Pi archive is added.
2. **Loop-vs-move coupling** — `move`/`swap` operate on `BT.WAV` only; a
   `LOOP.WAV` stays pinned to its physical `/NNtrack/`. So moving a backing track
   *does not* move its loop, and after a move a slot's loop may belong to a
   different track. **Open decision:** accept as a documented limitation, or make
   move/swap carry the loop. Deferred out of v1.
3. **Verified offload / real "backup"** — if the tool should ever *keep* loops
   on the Pi and let the user delete from the pedal with a safety net, that
   `remove` must gate on a **hash-verified** Pi copy (copy → `fsync` → compare →
   then `unlink`). Cheap (the bytes are already being read) but out of v1 scope,
   and it is the only thing that would justify calling the feature "backup".
