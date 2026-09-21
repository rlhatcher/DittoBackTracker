"""The service: pedal lifecycle, the library, and the convert/write queue."""

from __future__ import annotations

import itertools
import logging
import os
import queue
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import __version__, config, db, media, pedal
from .update import Updater

log = logging.getLogger(__name__)

_STOP = ("stop",)       # queued by shutdown() to wake the worker's get() at once


def mmss(seconds: float) -> str:
    m, s = divmod(int(max(seconds, 0)), 60)
    return f"{m}:{s:02d}"


class Service:
    def __init__(self) -> None:
        config.ensure_dirs()
        self._sweep()

        self._work: "queue.Queue[tuple]" = queue.Queue()
        self._subs: List["queue.Queue[dict]"] = []
        self._subs_lock = threading.Lock()

        self.pedal_state = "absent"          # absent | mounted | error
        self.busy: Optional[str] = None      # what the worker is doing
        self.progress: Optional[float] = None
        self.last_error: Optional[str] = None
        # Slots holding a LOOP.WAV, scanned once per mount: 99 stats over USB
        # per snapshot would be visibly slow.
        self._loops: "frozenset[int]" = frozenset()

        self._stop = threading.Event()
        # Held by the worker around every job and by the updater for a deploy,
        # so the two never overlap. A successful update keeps it: a restart is
        # pending and no job may touch the pedal under it.
        self._job_lock = threading.Lock()
        self.updater = Updater(job_lock=self._job_lock,
                               is_busy=self._busy_for_update,
                               stopped=self._stop.is_set,
                               on_change=self._emit)
        # Serializes library mutations: a forced forget clears a track's slots
        # and deletes its row as one step, and an upload or assign must not
        # land between. Reentrant because forget calls clear.
        self._lock = threading.RLock()
        # Set from dequeue to completion, so a drain sees a job the busy label
        # has not caught up with yet.
        self._in_flight = threading.Event()
        # Stamps snapshots, so a stream can drop one that arrives out of order.
        self._snap_seq = itertools.count(1)
        self._last_prog_emit = 0.0

        self._threads = [
            threading.Thread(target=self._worker, daemon=True, name="worker"),
            threading.Thread(target=self._monitor, daemon=True, name="monitor"),
            threading.Thread(target=self._startup_update_check, daemon=True,
                             name="updatecheck"),
        ]
        for t in self._threads:
            t.start()

    # ---------------------------------------------------------------- events

    def subscribe(self) -> "queue.Queue[dict]":
        q: "queue.Queue[dict]" = queue.Queue(maxsize=64)
        with self._subs_lock:
            self._subs.append(q)
        return q

    def unsubscribe(self, q) -> None:
        with self._subs_lock:
            if q in self._subs:
                self._subs.remove(q)

    def _emit(self) -> None:
        """Publish a snapshot to every open stream. Each frame is the whole
        state, so a full queue drops its backlog rather than the new frame."""
        snap = self.snapshot()
        with self._subs_lock:
            for q in list(self._subs):
                try:
                    q.put_nowait(snap)
                except queue.Full:
                    try:
                        while True:
                            q.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        q.put_nowait(snap)
                    except queue.Full:
                        pass

    # ---------------------------------------------------------------- state

    def capacity(self, slots: Optional[List[Dict]] = None) -> Dict:
        """Used and total time on the pedal. Mounted, from the bytes on the
        volume, so loops count; unmounted, from the assigned durations."""
        free, total = pedal.capacity()
        rate = config.BYTES_PER_SECOND
        if total:
            used_secs = (total - free) / rate
            total_secs = total / rate
        else:
            if slots is None:
                slots = db.all_slots()
            used_secs = sum(s["duration"] for s in slots)
            total_secs = 0
        return {
            "used_seconds": used_secs,
            "total_seconds": total_secs,
            "used_label": mmss(used_secs),
            "total_label": mmss(total_secs) if total_secs else "--:--",
        }

    def snapshot(self) -> Dict:
        slots = db.all_slots()      # read once; this runs at 5 Hz mid-conversion
        return {
            "seq": next(self._snap_seq),
            "pedal": self.pedal_state,
            "busy": self.busy,
            "progress": self.progress,
            "error": self.last_error,
            "slots": slots,
            "slot_count": config.SLOTS,
            "loops": sorted(self._loops),
            "capacity": self.capacity(slots),
            "version": __version__,
            "revision": self.updater.revision,
            "update_available": self.updater.available,
            "remote_revision": self.updater.remote_revision,
        }

    # ------------------------------------------------------------ operations

    @staticmethod
    def check_slot(slot: int) -> None:
        if not (1 <= slot <= config.SLOTS):
            raise ValueError(f"slot must be 1-{config.SLOTS}")

    def upload(self, slot: int, tmp_path: Path, display_name: str) -> Dict:
        """Ingest a file and put it in a slot. One lock over the row, the
        bytes and the assignment, so a forced forget cannot delete the row in
        between and leave a slot pointing at nothing."""
        self.check_slot(slot)
        with self._lock:
            h = self._take(tmp_path, display_name)
            self._assign(slot, h)
            row = db.get_slot(slot)
        self._emit()
        return row

    def add_to_library(self, tmp_path: Path, display_name: str) -> Dict:
        """Ingest a file without giving it a slot."""
        with self._lock:
            h = self._take(tmp_path, display_name)
            row = db.library_get(h)
        self._emit()
        return row

    def _take(self, tmp_path: Path, display_name: str) -> str:
        """Probe, hash and store an upload, under the caller's lock. Returns
        the hash. ValueError if ffprobe cannot read it."""
        info = media.probe(tmp_path)
        if info is None or info.duration <= 0:
            tmp_path.unlink(missing_ok=True)
            raise ValueError("not a readable audio file")
        h = media.file_hash(tmp_path)
        stored = config.SOURCES / f"{h}{tmp_path.suffix.lower() or '.bin'}"
        self._place_source(tmp_path, stored)
        db.library_add(h, display_name, info.duration)
        return h

    def assign(self, slot: int, source_hash: str) -> Optional[Dict]:
        """Put a track already in the library into a slot."""
        self.check_slot(slot)
        with self._lock:
            if not db.hash_in_library(source_hash):
                return None
            self._assign(slot, source_hash)
            row = db.get_slot(slot)
        self._emit()
        return row

    def _assign(self, slot: int, source_hash: str) -> None:
        """Point a slot at a library track and queue the work to realise it."""
        with self._lock:
            old = db.get_slot(slot)
            db.put_slot(slot, source_hash, state="converting")
            if old and old["source_hash"] != source_hash:
                self._drop_staged_if_unused(old["source_hash"])
            src = self.source_for(source_hash)
            if src is None:
                db.set_state(slot, "error", "source file missing")
                return
            self._work.put(("convert", slot, source_hash, src))

    def _plan(self, hashes: List[str]) -> Optional[Dict]:
        """Which slots these library tracks would fill, in the order given:
        consecutive from the first slot with room, skipping any that holds a
        loop, the rule an unnumbered upload follows. `start` and `end` are the
        first and last slot written, so they span the skips. None if any hash
        is unknown: the caller's list is stale, so nothing is planned around
        the gap."""
        tracks = []
        for h in hashes:
            row = db.library_get(h)
            if row is None:
                return None
            tracks.append(row)
        loops = self._loops
        taken = {s["slot"] for s in db.all_slots()} | loops
        start = next((n for n in range(1, config.SLOTS + 1) if n not in taken),
                     config.SLOTS + 1)
        assigned: List[Dict] = []
        unplaced: List[Dict] = []
        n = start
        for t in tracks:
            while n <= config.SLOTS and n in loops:
                n += 1
            if n > config.SLOTS:
                unplaced.append({"source_hash": t["source_hash"],
                                 "name": t["name"],
                                 "error": f"no room past slot {config.SLOTS}"})
                continue
            assigned.append({"slot": n, "source_hash": t["source_hash"],
                             "name": t["name"]})
            n += 1
        end = assigned[-1]["slot"] if assigned else None
        return {
            "start": assigned[0]["slot"] if assigned else None,
            "end": end,
            "assigned": assigned,
            "skipped_loops": sorted(x for x in loops
                                    if end is not None and start <= x <= end),
            "unplaced": unplaced,
            # The loop set is only known while mounted, so an unmounted plan
            # says its range is provisional.
            "loops_known": self.pedal_state == "mounted",
        }

    def assign_tracks(self, hashes: List[str]) -> Optional[Dict]:
        """Put a run of library tracks on the pedal as one locked step, with
        one snapshot at the end rather than one per track."""
        with self._lock:
            plan = self._plan(hashes)
            if plan is None:
                return None
            for item in plan["assigned"]:
                self._assign(item["slot"], item["source_hash"])
        if plan["assigned"]:
            self._emit()
        return plan

    def clear(self, slot: int) -> None:
        self.check_slot(slot)
        with self._lock:
            row = db.get_slot(slot)
            db.delete_slot(slot)
            self._work.put(("erase", slot))
            if row:
                self._drop_staged_if_unused(row["source_hash"])
        self._emit()

    @property
    def mounted(self) -> bool:
        """The monitor's view, refreshed every POLL_SECS. Every path that
        touches the pedal re-checks for itself before any I/O."""
        return self.pedal_state == "mounted"

    def has_loop(self, slot: int) -> bool:
        return slot in self._loops

    def loop_path(self, slot: int) -> Path:
        return pedal.loop_path(slot)

    def delete_loop(self, slot: int) -> Optional[bool]:
        """Remove a LOOP.WAV. One unlink, done here under the job lock so it
        never lands in the middle of a write. False (404) if there is none,
        None (409) while a job holds the lock: the worker keeps it for a whole
        write, and a request thread must not sit behind that."""
        self.check_slot(slot)
        if slot not in self._loops:
            return False
        if not self._job_lock.acquire(blocking=False):
            return None
        try:
            if not pedal.mounted():
                return False
            pedal.remove_loop(slot)
            os.sync()
            self._loops = self._loops - {slot}
        finally:
            self._job_lock.release()
        self._emit()
        return True

    def move(self, src: int, dst: int) -> None:
        """Move to an empty slot, or swap with an occupied one, so reordering
        never destroys anything."""
        self.check_slot(src)
        self.check_slot(dst)
        with self._lock:
            op = db.move_or_swap(src, dst)
            if op == "swap":
                self._work.put(("write", src))
                self._work.put(("write", dst))
            elif op == "move":
                self._work.put(("erase", src))
                self._work.put(("write", dst))
            else:
                return
        self._emit()

    # -------------------------------------------------------------- library

    def rename(self, source_hash: str, name: str) -> Optional[Dict]:
        if not db.library_rename(source_hash, name):
            return None
        self._emit()
        return db.library_get(source_hash)

    def forget(self, source_hash: str,
               force: bool = False) -> "tuple[str, List[int]]":
        """Delete a track and its audio.

        Returns (outcome, slots): "in_use" refused, nothing changed; "deleted";
        or "missing", no such row but the slots pointing at it were still
        cleared. Refuses while a slot holds the track unless forced.
        """
        with self._lock:
            slots = db.slots_for_hash(source_hash)
            if slots and not force:
                return ("in_use", slots)
            for n in slots:
                self.clear(n)
            deleted = db.library_delete(source_hash)
            src = self.source_for(source_hash)
            if src:
                src.unlink(missing_ok=True)
            media.staged_path(source_hash).unlink(missing_ok=True)
        self._emit()
        return ("deleted" if deleted else "missing", slots)

    def _drop_staged_if_unused(self, source_hash: str) -> None:
        """The staged WAV is a cache bounded by the slots, not the library."""
        if not db.slots_for_hash(source_hash):
            media.staged_path(source_hash).unlink(missing_ok=True)

    def _busy_for_update(self) -> bool:
        """Work in flight or queued. Asked with the job lock held, so a running
        job has already refused; this is for one waiting or still queued."""
        return bool(self.busy or self._in_flight.is_set()
                    or not self._work.empty())

    def update(self) -> "tuple[bool, str]":
        return self.updater.update()

    def check_now(self) -> Dict:
        return self.updater.check_now()

    def _startup_update_check(self) -> None:
        self.updater.startup_check()

    def shutdown(self, timeout: float = 30.0) -> None:
        """SIGTERM path. Finish the queue if it is quick, stop the threads,
        unmount. Anything still queued is requeued on the next mount."""
        if self._stop.is_set():
            return
        self._drain(timeout=timeout)
        self._stop.set()
        self._work.put(_STOP)
        for t in self._threads:
            t.join(timeout=5.0)
        try:
            os.sync()
            pedal.unmount()
        except Exception:
            log.exception("unmount during shutdown failed")

    # -------------------------------------------------------------- internals

    def _place_source(self, tmp_path: Path, stored: Path) -> None:
        """Get the bytes into sources/ before any row names them. A file with
        no row is swept at boot; a row with no file is an error the user has
        to clear by hand."""
        if stored.exists():
            tmp_path.unlink(missing_ok=True)
            return
        self._store_source(tmp_path, stored)

    @staticmethod
    def _store_source(tmp_path: Path, stored: Path) -> None:
        """Sync the bytes, then publish the name. sources/ is content-addressed,
        so a name over unconfirmed bytes would be adopted by the next upload of
        the same track instead of rewritten."""
        try:
            with open(tmp_path, "rb") as f:
                os.fsync(f.fileno())
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise
        tmp_path.replace(stored)

    def source_for(self, h: str) -> Optional[Path]:
        for p in config.SOURCES.glob(f"{h}.*"):
            return p
        return None

    def _sweep(self) -> None:
        """Reclaim what the last run left behind: interrupted transcodes,
        stranded upload temporaries, and staged or source files nothing
        references. Runs once, at boot, when nothing is in flight."""
        try:
            for f in config.STAGED.glob("*.wav.part"):
                f.unlink(missing_ok=True)
            for f in config.DATA.glob("tmp*"):
                if f.is_file():
                    f.unlink(missing_ok=True)
            assigned = {r["source_hash"] for r in db.all_slots()}
            for f in config.STAGED.glob("*.wav"):
                if f.stem not in assigned:
                    f.unlink(missing_ok=True)
            known = {r["source_hash"] for r in db.library_all()}
            for f in config.SOURCES.iterdir():
                if f.is_file() and f.stem not in known:
                    f.unlink(missing_ok=True)
        except Exception:
            log.exception("startup sweep failed")

    def _monitor(self) -> None:
        while not self._stop.is_set():
            try:
                self._tick_pedal()
            except Exception as e:      # never let the monitor die
                log.exception("monitor tick failed")
                self.last_error = str(e)
            self._stop.wait(config.POLL_SECS)

    def _tick_pedal(self) -> None:
        if self._stop.is_set():
            return
        if not pedal.present():
            if self.pedal_state != "absent":
                self.pedal_state = "absent"
                self._loops = frozenset()   # only knowable mounted
                self._emit()
            return
        try:
            pedal.mount()               # no-op if already mounted
        except pedal.PedalError as e:
            if self.pedal_state != "error":
                self.pedal_state = "error"
                self._loops = frozenset()
                self.last_error = str(e)
                self._emit()
            return
        if self.pedal_state != "mounted":
            pedal.clean_temp_files()
            self._scan_loops()
            # Published after the scan: upload_auto and _plan take "mounted"
            # as their licence to trust _loops.
            self.last_error = None
            self.pedal_state = "mounted"
            self._requeue_unsynced()
            self._emit()

    def _requeue_unsynced(self) -> None:
        on_pedal = pedal.occupied_slots()
        for row in db.all_slots():
            slot, h = row["slot"], row["source_hash"]
            if row["state"] == "error":
                continue
            if row["synced_hash"] != h or slot not in on_pedal:
                self._work.put(("write", slot))

    def _scan_loops(self) -> None:
        self._loops = frozenset(
            n for n in range(1, config.SLOTS + 1) if pedal.has_loop(n))

    # ------------------------------------------------------------ work queue

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._work.get(timeout=0.5)
            except queue.Empty:
                continue
            if job is _STOP:
                break
            self._in_flight.set()
            try:
                # A pending restart holds the lock for good, so wait in short
                # steps and let a stop through.
                while not self._job_lock.acquire(timeout=0.5):
                    if self._stop.is_set():
                        return
                try:
                    self._run_job(job)
                except Exception as e:
                    log.exception("job %r failed", job[0])
                    self.last_error = str(e)
                finally:
                    self._job_lock.release()
            finally:
                self._in_flight.clear()
                self.busy = None
                self.progress = None
                self._emit()

    def _run_job(self, job) -> None:
        kind = job[0]
        if kind == "convert":
            _, slot, h, src = job
            self._do_convert(slot, h, src)
        elif kind == "write":
            self._do_write(job[1])
        elif kind == "erase":
            self._do_erase(job[1])

    def _do_convert(self, slot: int, h: str, src: Path) -> None:
        row = db.get_slot(slot)
        if not row or row["source_hash"] != h:
            return          # superseded by a newer upload
        self.busy = f"Converting {row['display_name']}"
        self.progress = 0.0
        self._emit()

        def prog(f):
            self.progress = f
            # ffmpeg reports several times a second and each emit rebuilds a
            # snapshot on a Pi Zero, so cap it to 5 Hz.
            now = time.monotonic()
            if now - self._last_prog_emit >= 0.2:
                self._last_prog_emit = now
                self._emit()

        try:
            media.convert(src, h, progress=prog, duration=row["duration"])
        except media.ConvertError as e:
            db.set_state(slot, "error", str(e))
            self._emit()
            return
        db.set_state(slot, "staged")
        self._emit()
        self._work.put(("write", slot))

    def _do_write(self, slot: int) -> None:
        row = db.get_slot(slot)
        if not row or row["state"] == "error":
            return
        if not pedal.mounted():
            return          # stays staged; written when the pedal appears

        wav = media.staged_path(row["source_hash"])
        if not wav.exists():
            src = self.source_for(row["source_hash"])
            if src:
                self._work.put(("convert", slot, row["source_hash"], src))
            else:
                db.set_state(slot, "error", "converted file missing")
            self._emit()
            return

        need = wav.stat().st_size
        free, _ = pedal.capacity()
        existing = pedal.track_path(slot)
        if existing.is_file():
            free += existing.stat().st_size
        if need > free:
            short = mmss((need - free) / config.BYTES_PER_SECOND)
            db.set_state(slot, "error", f"won't fit — over capacity by {short}")
            self._emit()
            return

        self.busy = f"Writing {row['display_name']}"
        self.progress = 0.0
        self._emit()
        try:
            pedal.write_track(slot, wav)
        except OSError as e:
            db.set_state(slot, "error", f"write failed: {e}")
            self._emit()
            return
        os.sync()       # before the database says it is on the pedal
        db.mark_synced(slot, row["source_hash"])
        self._emit()

    def _do_erase(self, slot: int) -> None:
        if not pedal.mounted():
            return
        if db.get_slot(slot):
            return          # slot was refilled before we got here
        self.busy = f"Clearing slot {slot:02d}"
        self._emit()
        try:
            pedal.remove_track(slot)
            os.sync()
        except OSError as e:
            self.last_error = f"could not clear slot {slot}: {e}"
        self._emit()

    def _drain(self, timeout: float = 300.0) -> None:
        """Wait for the worker to go idle. Only the SIGTERM path uses it."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._work.empty() and not self._in_flight.is_set():
                return
            time.sleep(0.05)
