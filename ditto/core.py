"""The service: pedal lifecycle, transcode queue, write queue."""

from __future__ import annotations

import contextlib
import itertools
import logging
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from . import __version__, config, db, media, pedal

log = logging.getLogger(__name__)


class ShuttingDown(Exception):
    """A pedal operation was asked for after the session began ending."""


def mmss(seconds: float) -> str:
    m, s = divmod(int(max(seconds, 0)), 60)
    return f"{m}:{s:02d}"


def local_ip() -> str:
    """Best-effort local address, for the web UI. No traffic is sent."""
    try:
        # Context-managed so the socket closes even if connect/getsockname
        # raises; local_ip runs on every snapshot, so a leak would accumulate.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
    except Exception:
        return "no network"


class LoopStage:
    """Coordination handle for the blocking loop-download bridge between a web
    thread (the waiter) and the worker.

    It closes the timeout/publish handoff: the waiter giving up and the worker
    finishing the copy resolve under one lock, so exactly one side owns the
    staged file and it is never orphaned. `done` is set by the worker when the
    outcome is settled; the waiter blocks on it with a timeout.
    """

    def __init__(self) -> None:
        self.done = threading.Event()
        self.path: Optional[str] = None      # set once, by whoever the worker
        self.error: Optional[str] = None     # hands the outcome to
        self._cancel = False
        self._lock = threading.Lock()

    def cancelled(self) -> bool:
        with self._lock:
            return self._cancel

    def publish(self, path: str) -> bool:
        """Worker: hand `path` to the waiter. Returns True if accepted; False if
        the waiter already gave up, in which case the worker must purge `path`."""
        with self._lock:
            if self._cancel:
                return False
            self.path = path
            return True

    def cancel_and_reap(self) -> Optional[str]:
        """Waiter: give up. Returns an already-published path for the waiter to
        purge (else None); after this, publish() will refuse and the worker
        purges its own file."""
        with self._lock:
            self._cancel = True
            return self.path


class Service:
    def __init__(self) -> None:
        config.ensure_dirs()
        # Staged loop copies are transient; clear any left behind by a crash
        # mid-download so nothing accumulates on the data partition.
        self._purge_staged_loops()

        self._work: "queue.Queue[tuple]" = queue.Queue()
        self._subs: List["queue.Queue[dict]"] = []
        self._subs_lock = threading.Lock()

        self.fmt: Dict = dict(config.DEFAULT_FORMAT)
        self.fmt_source = "default"
        self.pedal_state = "absent"          # absent | mounted | error
        self.busy: Optional[str] = None      # human-readable current activity
        # What kind of pedal work `busy` is: "write" | "read" | None. Unplugging
        # mid-read is low-risk, so only a write earns the UI's "don't unplug"
        # warning — the job that used to belong to the OLED.
        self.busy_kind: Optional[str] = None
        self.progress: Optional[float] = None
        self.last_error: Optional[str] = None
        self.ending = False

        # Slots that hold a LOOP.WAV, scanned once on mount (never per _emit —
        # 99 stats over 1 MB/s USB per SSE frame would be visibly slow). A
        # frozenset reassigned on change, so snapshot() readers on other threads
        # always see a consistent set without a lock.
        self._loops: "frozenset[int]" = frozenset()
        # Bumped on every transition into the mounted state. A loop job captures
        # it at enqueue; the worker skips the job if it no longer matches, so a
        # job queued in one plug-in session never reads or deletes a LOOP.WAV
        # from a different pedal mounted after an unplug/replug. Only the monitor
        # thread writes it; the worker only reads (an atomic int compare).
        self._mount_gen = 0
        # Serializes self-update so two clicks can't redeploy on top of each
        # other. Held only for the brief git-pull + redeploy, never a job.
        self._update_lock = threading.Lock()
        # Set while an update is admitted: gates the worker from starting any new
        # job, so a redeploy/restart never overlaps pedal work. Stays set once a
        # restart is pending; cleared if the update bails out.
        self._updating = threading.Event()

        # Deployed code identity + whether the remote has something newer.
        # `revision`/`_current_sha` are the SHA actually deployed (recorded at
        # the last successful deploy, else the checkout HEAD) — cheap, no network
        # — read once at startup; `update_available`/`remote_revision` are set by
        # the startup check and by on-demand checks the user triggers.
        self._revision, self._current_sha = self._deployed_head()
        self._update_available = False
        self._remote_revision: Optional[str] = None

        # Serializes "is this device still accepting work?" with the enqueue
        # that follows it, and with end_session's own set-and-enqueue. Reentrant
        # because grouped operations (a forced library delete clearing several
        # slots) admit once and then call through to per-slot operations that
        # admit again.
        self._admit = threading.RLock()

        # Set while the worker holds a job whose busy flag isn't up yet, so a
        # drain can't mistake the gap between dequeue and write for "idle".
        self._in_flight = threading.Event()
        # Stamps every snapshot so a consumer can order them. _emit builds its
        # snapshot before it takes the subscriber lock, so a frame can reach a
        # queue after a newer one was built elsewhere — the sequence is what
        # lets the reader drop it instead of rendering stale state.
        # next() on an itertools.count is atomic, so no lock is needed.
        self._snap_seq = itertools.count(1)
        self._last_prog_emit = 0.0

        self._stop = threading.Event()
        # One update check at startup, off the boot path so it never delays the
        # app coming up (and no-ops without a checkout or network). After that,
        # checks happen only when the user asks. Tracked in _threads so shutdown()
        # joins it rather than leaving it to emit during teardown.
        # Sweep whatever the last run left behind. _gc otherwise runs only from
        # _halt(), which a pulled plug never reaches — so without this, every
        # ungraceful stop leaks interrupted transcodes and upload temporaries
        # onto a small data partition until the next clean shutdown. Queued
        # rather than run inline so it stays off the boot path.
        self._work.put(("gc",))

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
        """Publish a snapshot to every open stream, newest-wins.

        Each frame is the whole state, so a queued older frame is worthless the
        moment a newer one exists. Dropping the *new* frame on a full queue —
        which is what a bare put_nowait does — serves a slow client (a phone
        that slept) stale state until something else emits, which for a one-shot
        change like an error or update_available can be a long time. Discard the
        backlog instead.
        """
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
                        pass        # the reader refilled it; it has fresh state

    # ---------------------------------------------------------------- state

    def capacity(self) -> Dict:
        free, total = pedal.capacity()
        rate = media.bytes_per_second(self.fmt)
        if total and rate:
            # Count real bytes on the volume, not summed DB durations: this
            # includes LOOP.WAV (and anything else physically present), so the
            # gauge stops overstating free time the moment loops exist. FAT
            # cluster slack errs toward showing less free — the safe direction.
            used_secs = (total - free) / rate
            total_secs = total / rate
        else:
            # Unmounted: no byte figures to trust, fall back to DB durations.
            used_secs = sum(s["duration"] for s in db.all_slots())
            total_secs = 0
        return {
            "bytes_free": free, "bytes_total": total,
            "rate": rate,
            "used_seconds": used_secs,
            "total_seconds": total_secs,
            "used_label": mmss(used_secs),
            "total_label": mmss(total_secs) if total_secs else "--:--",
        }

    def snapshot(self) -> Dict:
        return {
            "seq": next(self._snap_seq),
            "pedal": self.pedal_state,
            "busy": self.busy,
            "busy_kind": self.busy_kind,
            "progress": self.progress,
            "ending": self.ending,
            "error": self.last_error,
            "format": self.fmt,
            "format_source": self.fmt_source,
            "slots": db.all_slots(),
            "slot_count": config.SLOTS,
            "loops": sorted(self._loops),
            "capacity": self.capacity(),
            "ip": local_ip(),
            "version": __version__,
            "revision": self._revision,
            "update_available": self._update_available,
            "remote_revision": self._remote_revision,
        }

    # ------------------------------------------------------------ operations

    @staticmethod
    def _check_slot(slot: int) -> None:
        if not (1 <= slot <= config.SLOTS):
            raise ValueError(f"slot must be 1-{config.SLOTS}")

    def _check_accepting(self) -> None:
        """Cheap early refusal, so a doomed upload isn't probed and hashed first.

        Advisory only — `_admitting` is what actually decides.
        """
        if self.ending:
            raise ShuttingDown("the device is shutting down")

    @contextlib.contextmanager
    def _admitting(self):
        """Decide and enqueue as one step.

        Testing `ending` and then queueing work is not enough on its own: an
        upload spends seconds probing and hashing between the two, and
        end_session can land in that gap — so the work goes in behind an end
        marker that has already passed, and poweroff discards it after the API
        said 201.

        Callers do their slow part — probing, hashing — outside this block.
        Ingest is the deliberate exception: it holds the admission across
        writing the source file too, because the library row, the bytes and the
        slot assignment have to land as one step or a concurrent forced forget
        can delete the row in between and leave a slot pointing at nothing. That
        does mean end_session waits on an fsync, which is the behaviour we want:
        poweroff should not begin midway through storing a track.
        """
        with self._admit:
            if self.ending:
                raise ShuttingDown("the device is shutting down")
            yield

    def upload(self, slot: int, tmp_path: Path, display_name: str) -> Dict:
        self._check_accepting()
        self._check_slot(slot)

        info = media.probe(tmp_path)
        if info is None or info.duration <= 0:
            tmp_path.unlink(missing_ok=True)
            raise ValueError("not a readable audio file")

        h = media.file_hash(tmp_path)
        # _source_for globs for "{hash}.*", so an extensionless upload would be
        # stored under a name it can never find again.
        stored = config.SOURCES / f"{h}{tmp_path.suffix.lower() or '.bin'}"
        # The row before the bytes. The two orderings fail differently and only
        # one of them fails safely: a row with no file surfaces as "source file
        # missing" on convert, which is visible and fixable, whereas a file with
        # no row is invisible and the collector eventually takes it.
        # One admission over the whole mutation. Splitting it would leave a
        # window in which a forced forget deletes the row between library_add
        # and _assign — the slot would then reference a hash with no library
        # row, reading as "(missing)", and the next collector pass would take
        # its source file. Reading the row back inside the block keeps the
        # returned object consistent with what was just committed.
        with self._admitting():
            inserted = db.library_add(h, display_name, info.duration)
            self._place_source(h, tmp_path, stored, inserted)
            self._assign(slot, h)
            row = db.get_slot(slot)
        self._emit()
        return row

    def add_to_library(self, tmp_path: Path, display_name: str) -> Dict:
        """Ingest a file without giving it a slot.

        The pedal holds about twelve tracks and the library holds as many as the
        card does, so getting audio onto the device and choosing what the pedal
        carries are separate acts.
        """
        info = media.probe(tmp_path)
        if info is None or info.duration <= 0:
            tmp_path.unlink(missing_ok=True)
            raise ValueError("not a readable audio file")

        h = media.file_hash(tmp_path)
        stored = config.SOURCES / f"{h}{tmp_path.suffix.lower() or '.bin'}"
        # Admitted like every other mutation. Without this, end_session could
        # begin the halt while the source file was still being written and this
        # would still report success.
        with self._admitting():
            inserted = db.library_add(h, display_name, info.duration)
            self._place_source(h, tmp_path, stored, inserted)
            row = db.library_get(h)
        self.library_changed()
        return row

    def assign(self, slot: int, source_hash: str) -> Optional[Dict]:
        """Put a track already in the library into a slot.

        The point of the library: rearranging what the pedal carries costs a
        transcode at most, and usually not even that — the staged WAV may still
        be cached — instead of another upload over WiFi.
        """
        self._check_accepting()
        self._check_slot(slot)
        # Inside the admission, not before it. forget() holds the same lock
        # across reading the slot list, clearing those slots and deleting the
        # row — so a membership test outside it can pass, then have the track
        # deleted before the assignment lands. forget has already read its slot
        # list by then, so it would never clear the new slot, and the collector
        # would take the source out from under it: a slot reading "(missing)"
        # with no audio behind it. restore() already checks under the lock.
        with self._admitting():
            if not db.hash_in_library(source_hash):
                return None
            self._assign(slot, source_hash)
            # Read it back inside the block, like upload does. A concurrent
            # forced forget can clear the slot the moment the lock is released,
            # and a None here becomes a 404 for an assignment that did happen.
            row = db.get_slot(slot)
        self._emit()
        return row

    def _assign(self, slot: int, source_hash: str) -> None:
        """Point a slot at a library track and queue the work to realise it.

        Admits here rather than in each caller: this is the single place all
        three routes into a slot — upload, restore and assign — change the
        database and queue work. The lock is reentrant, so a caller that has
        already admitted (to keep a larger step atomic) nests harmlessly.
        """
        with self._admitting():
            if db.get_slot(slot):
                db.delete_slot(slot, to_trash=True)
            db.put_slot(slot, source_hash, state="converting")
            src = self._source_for(source_hash)
            if src is None:
                db.set_state(slot, "error", "source file missing")
                return
            self._work.put(("convert", slot, source_hash, src))

    def clear(self, slot: int) -> Optional[int]:
        self._check_slot(slot)
        with self._admitting():
            trash_id = db.delete_slot(slot, to_trash=True)
            self._work.put(("erase", slot))
        self._emit()
        return trash_id

    def has_loop(self, slot: int) -> bool:
        """From the mount-time cache — no pedal I/O. Only meaningful mounted."""
        return slot in self._loops

    def stage_loop(self, slot: int) -> LoopStage:
        """Enqueue a copy of the slot's loop off the pedal into loops/ and return
        a LoopStage the caller blocks on.

        Blocking bridge: the worker stages to a path unique to this request and
        hands the outcome to the returned handle. If the caller times out it
        calls cancel_and_reap(); the handle guarantees exactly one side owns the
        staged file, so nothing is orphaned. The job carries the current mount
        generation so a stale job (queued before an unplug/replug) is skipped.
        """
        self._check_slot(slot)
        stage = LoopStage()
        with self._admitting():
            self._work.put(("stage_loop", slot, self._mount_gen, stage))
        self._emit()
        return stage

    def delete_loop(self, slot: int) -> bool:
        """Enqueue removal of the slot's loop. False (→404) if none is known."""
        # Ahead of the no-op return, so the answer doesn't depend on whether
        # the slot happened to hold a loop: during shutdown this is refused
        # either way.
        self._check_accepting()
        self._check_slot(slot)
        if slot not in self._loops:
            return False
        with self._admitting():
            self._work.put(("delete_loop", slot, self._mount_gen))
        self._emit()
        return True

    def move(self, src: int, dst: int) -> None:
        """Move to an empty slot, or swap with an occupied one.

        Swapping rather than overwriting means reordering a setlist never
        destroys anything, so no trash entry and no undo needed.
        """
        self._check_slot(src)
        self._check_slot(dst)
        # The move-or-swap decision is made atomically in db, so we queue pedal
        # work from what actually happened rather than a pre-read that a
        # concurrent upload could have invalidated.
        with self._admitting():
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

    def retry(self, slot: int) -> None:
        self._check_accepting()     # ahead of the empty-slot return, as above
        self._check_slot(slot)
        row = db.get_slot(slot)
        if not row:
            return
        src = self._source_for(row["source_hash"])
        if not src:
            db.set_state(slot, "error", "source file missing")
            self._emit()
            return
        with self._admitting():
            db.set_state(slot, "converting")
            self._work.put(("convert", slot, row["source_hash"], src))
        self._emit()

    def restore(self, trash_id: int) -> Optional[int]:
        with self._admitting():
            item = db.trash_pop(trash_id)
            if not item:
                return None
            # The track can have been deleted from the library since the slot
            # was cleared, in which case there is nothing left to put back.
            if not db.hash_in_library(item["source_hash"]):
                return None
            self._assign(item["slot"], item["source_hash"])
        self._emit()
        return item["slot"]

    # -------------------------------------------------------------- library

    def rename(self, source_hash: str, name: str) -> Optional[Dict]:
        """Rename a track. One row changes; the slot list, the grid tooltips and
        the printed set list all read through to it."""
        if not db.library_rename(source_hash, name):
            return None
        self.library_changed()
        return db.library_get(source_hash)

    def forget(self, source_hash: str,
               force: bool = False) -> "tuple[str, List[int]]":
        """Delete a track from the library, and with it the only copy of its
        audio.

        Returns (outcome, slots). Three outcomes, because "it didn't get
        deleted" and "nothing happened" are not the same thing and the caller
        has to tell them apart:

          "in_use"  — refused, nothing changed; `slots` hold the track
          "deleted" — gone; `slots` are the ones cleared on the way
          "missing" — no such row, but `slots` were still cleared

        The one operation that can pull a file out from under a slot, so it
        refuses while any slot references the track unless the caller has
        confirmed. The audio itself goes on the next collector pass, once the
        row that was keeping it alive is gone.
        """
        with self._admitting():
            slots = db.slots_for_hash(source_hash)
            if slots and not force:
                return ("in_use", slots)
            # One admission covering the whole group, so a shutdown can't land
            # between clearing some slots and deleting the row.
            for n in slots:
                self.clear(n)
            deleted = db.library_delete(source_hash)
        # Either branch may have cleared slots, so both report them and both
        # emit. Only the refusal above leaves the device untouched.
        self.library_changed()
        return ("deleted" if deleted else "missing", slots)

    def library_changed(self) -> None:
        """Single funnel for library mutations.

        A rename reaches the slot list on its own — display_name is joined from
        library, so the next snapshot already carries it. This is what tells
        clients the *library list* itself moved.
        """
        self._emit()

    def end_session(self) -> None:
        """Flush, unmount, halt. The only way to power the device off.

        The check, the flag and the marker go under one lock. Two of these
        racing would each queue a marker, and _run_job requeues a marker
        whenever the queue is non-empty — so the pair would step over each
        other indefinitely and the device would never power off. Emitting
        happens after the lock; it reads the database and has no business
        holding up a shutdown.
        """
        with self._admit:
            if self.ending:
                return
            self.ending = True
            self._work.put(("end",))
        self._emit()

    # -------------------------------------------------------------- self-update

    def update(self) -> "tuple[bool, str]":
        """Pull the tracked branch, redeploy the app, and restart out-of-process.

        Returns (ok, message): on success `message` is the deployed short commit,
        otherwise a human-readable reason. Serialized so two clicks can't redeploy
        on top of each other. Admission is atomic with job execution: `_updating`
        is set first, which gates the worker from starting any job, and the update
        is only admitted when nothing is in flight and the queue is empty — so a
        restart never runs concurrently with (or interrupts) a write. On success
        `_updating` stays set (a restart is pending); it's cleared on every path
        that does not initiate one. The restart itself is done by a separate
        oneshot unit (RESTART_SERVICE), so it isn't killing this process.
        """
        if self.ending:
            return (False, "the device is shutting down")
        if not self._update_lock.acquire(blocking=False):
            return (False, "an update is already running")
        # Gate the worker before inspecting idleness, so a job can't slip from
        # the queue into flight between the check and the deploy.
        self._updating.set()
        result = (False, "update failed")
        try:
            if self.busy or self._in_flight.is_set() or not self._work.empty():
                result = (False, "busy — try again when the current work finishes")
            else:
                result = self._do_update()
        finally:
            self._update_lock.release()
            if not result[0]:
                self._updating.clear()      # no restart pending — resume work
        return result

    def _do_update(self) -> "tuple[bool, str]":
        src, app = config.SRC, config.APP
        if not (src / ".git").is_dir():
            return (False, f"no git checkout at {src}")
        try:
            self._git(src, "fetch", "--quiet", "origin", config.UPDATE_BRANCH)
            self._git(src, "reset", "--hard", "--quiet",
                      f"origin/{config.UPDATE_BRANCH}")
        except (subprocess.SubprocessError, OSError) as e:
            return (False, f"git update failed: {self._proc_err(e)}")

        target = self._rev_parse(src, "HEAD")        # what we're deploying to

        # Atomic-ish redeploy: build the new tree beside the live one and swap by
        # rename, so a failed copy never leaves the app without a ditto/ package.
        # The previous deployment is kept as ditto.bak — a one-rename rollback if
        # the new code won't load, and the last-known-good between updates.
        new, live, bak = app / "ditto.new", app / "ditto", app / "ditto.bak"
        try:
            if new.exists():
                shutil.rmtree(new)
            shutil.copytree(src / "ditto", new)
            if bak.exists():
                shutil.rmtree(bak)
            if live.exists():
                live.rename(bak)
            new.rename(live)
        except OSError as e:
            if not live.exists() and bak.exists():
                bak.rename(live)                # put the old code back
            shutil.rmtree(new, ignore_errors=True)
            return (False, f"deploy failed: {e}")

        # Smoke-check the deployed code before committing to a restart: import it
        # from the app dir (cwd is first on sys.path). This catches a syntax or
        # import error — the common "broke on deploy" — without waiting on the
        # restart, which cannot be observed from the process being restarted. On
        # failure, roll back to the previous deployment and do not restart.
        check = self._import_check(app)
        if check is not None:
            err = self._rollback(live, bak)
            if err:
                return (False, f"new code failed to load and rollback failed "
                               f"({err}); manual recovery may be needed")
            return (False, f"new code failed to load; rolled back: {check}")

        # Record the SHA that is now actually deployed — only after the swap and
        # smoke-check succeed — so the reported revision and the update check
        # reflect the running tree even if a later deploy fails after the reset.
        # Write atomically (temp + replace) so a failed write leaves the previous
        # REVISION intact; if it fails, roll back so the recorded revision and the
        # live tree can never disagree on the next boot.
        if target:
            tmp = config.REVISION_FILE.with_name(config.REVISION_FILE.name + ".tmp")
            try:
                tmp.write_text(target + "\n")
                os.replace(str(tmp), str(config.REVISION_FILE))
            except OSError as e:
                tmp.unlink(missing_ok=True)
                err = self._rollback(live, bak)
                if err:
                    return (False, f"deployed but could not record the revision "
                                   f"({e}) and rollback failed ({err}); manual "
                                   f"recovery may be needed")
                return (False, f"deployed but could not record the revision "
                               f"({e}); rolled back")
        self._current_sha = target
        self._revision = target[:7] if target else None
        self._update_available = False

        # Restart from outside this process. sudo -n so a missing NOPASSWD rule
        # fails fast rather than hanging on a password prompt.
        try:
            # Absolute systemctl path so it matches the scoped sudoers rule
            # exactly (see etc/99-ditto-restart).
            subprocess.run(
                ["sudo", "-n", "/usr/bin/systemctl", "start", "--no-block",
                 config.RESTART_SERVICE],
                check=True, capture_output=True, text=True, timeout=15)
        except (subprocess.SubprocessError, OSError) as e:
            return (False, f"deployed {self._revision or 'update'} but the "
                           f"restart was refused — is OTA set up? "
                           f"{self._proc_err(e)}")
        return (True, self._revision or "updated")

    @staticmethod
    def _rollback(live: Path, bak: Path) -> Optional[str]:
        """Restore the previous deployment (bak -> live) as a checked operation.
        Returns None on success, or a short error if the live tree could not be
        removed or the backup could not be restored — in which case the caller
        must surface it rather than leaving broken code in place."""
        try:
            if live.exists():
                shutil.rmtree(live)
            if bak.exists():
                bak.rename(live)
        except OSError as e:
            return str(e)[:200]
        return None

    @staticmethod
    def _import_check(app: Path) -> Optional[str]:
        """Import the deployed package from `app`. Returns None if it loads, or a
        short error string if it doesn't."""
        try:
            r = subprocess.run([sys.executable, "-c", "import ditto.web"],
                               cwd=str(app), capture_output=True, text=True,
                               timeout=30)
        except (subprocess.SubprocessError, OSError) as e:
            return str(e)[:200]
        if r.returncode != 0:
            return (r.stderr.strip() or "import failed").splitlines()[-1][:200]
        return None

    def _startup_update_check(self) -> None:
        """One remote check at startup so the button reflects reality without the
        user asking. Off the boot path; no-ops without a checkout or network.
        After this, checks happen only on demand (check_now)."""
        self._check_for_update()

    def check_now(self) -> Dict:
        """Run a remote check on demand and report the outcome for the UI. Blocks
        on the git fetch (seconds on this single-user box). `ok` is false with a
        reason when the check couldn't run (no deployment, offline, mid-deploy)."""
        err = self._check_for_update()
        return {
            "ok": err is None,
            "error": err,
            "revision": self._revision,
            "update_available": self._update_available,
            "remote_revision": self._remote_revision,
        }

    def _check_for_update(self) -> Optional[str]:
        """Fetch the remote and flag whether it has something newer than the
        deployed checkout. Returns None when the check ran (state may have
        changed), or a short reason when it couldn't. Emits only when the result
        changes.

        Runs the whole check under _update_lock so it never runs git on the
        checkout concurrently with a deploy (update() holds the same lock): if a
        deploy holds it, the check just skips this round."""
        src = config.SRC
        if self._current_sha is None or not (src / ".git").is_dir():
            return "no deployment to check"
        if not self._update_lock.acquire(blocking=False):
            return "an update is already running"
        try:
            try:
                self._git(src, "fetch", "--quiet", "origin",
                          config.UPDATE_BRANCH)
            except (subprocess.SubprocessError, OSError):
                return "couldn't reach the remote"
            ref = f"origin/{config.UPDATE_BRANCH}"
            remote_full = self._rev_parse(src, ref)
            if not remote_full:
                return "couldn't read the remote branch"
            available = remote_full != self._current_sha
            # Contract: remote_revision is meaningful only when an update is
            # available; keep it null otherwise.
            remote_short = self._rev_parse(src, ref, short=True) if available else None
            # Publish under the lock: a deploy holds the same lock, so this can't
            # overwrite update_available against a _current_sha the deploy has
            # since moved. Skip during teardown.
            if self._stop.is_set():
                return None
            if (available != self._update_available
                    or remote_short != self._remote_revision):
                self._update_available = available
                self._remote_revision = remote_short
                self._emit()
        finally:
            self._update_lock.release()
        return None

    def _deployed_head(self) -> "tuple[Optional[str], Optional[str]]":
        """(short, full) SHA of the deployed code. Prefer the SHA recorded at the
        last successful deploy; fall back to the checkout HEAD (a fresh install
        has app == src but no REVISION yet). (None, None) if neither is known."""
        try:
            full = config.REVISION_FILE.read_text().strip() or None
        except OSError:
            full = None
        if not full and (config.SRC / ".git").is_dir():
            full = self._rev_parse(config.SRC, "HEAD")
        return (full[:7] if full else None, full)

    @staticmethod
    def _git(cwd: Path, *args: str) -> None:
        subprocess.run(["git", "-C", str(cwd), *args], check=True,
                       capture_output=True, text=True, timeout=60)

    @staticmethod
    def _rev_parse(src: Path, ref: str, short: bool = False) -> Optional[str]:
        args = ["rev-parse"] + (["--short"] if short else []) + [ref]
        try:
            r = subprocess.run(["git", "-C", str(src), *args], check=True,
                               capture_output=True, text=True, timeout=10)
            return r.stdout.strip() or None
        except (subprocess.SubprocessError, OSError):
            return None

    @staticmethod
    def _proc_err(e: Exception) -> str:
        out = getattr(e, "stderr", "") or ""
        return (out.strip() or str(e))[:200]

    def shutdown(self, timeout: float = 30.0) -> None:
        """SIGTERM path — a service stop or restart, not a session ending.

        Unlike _halt this never powers off, but it must still leave the pedal
        unmounted: systemd will SIGKILL us if we dawdle, so the drain is
        bounded and anything still queued is abandoned rather than waited on.
        """
        if self._stop.is_set():
            return
        self._drain(timeout=timeout)
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5.0)
        try:
            os.sync()
            pedal.unmount()
        except Exception:
            log.exception("unmount during shutdown failed")

    # -------------------------------------------------------------- internals

    def _place_source(self, h: str, tmp_path: Path, stored: Path,
                      inserted: bool) -> None:
        """Get the bytes into sources/, retracting the row if they don't land.

        The row is written before the bytes on purpose — a row with no file is
        visible and fixable, a file with no row is invisible and gets collected.
        But that only holds while the missing file is temporary. _store_source
        now raises when it cannot confirm the bytes, and nothing collects a
        library row: it would sit in the list forever, failing to audition and
        reporting "source file missing" on every assignment. So a storage
        failure takes the row with it.

        Only when this call created it. library_add is DO NOTHING on conflict,
        so re-uploading a track that is already in the library must not let a
        failure here delete the established row — and its file, which is still
        perfectly good, is exactly why the write was skipped.
        """
        if stored.exists():
            tmp_path.unlink(missing_ok=True)
            return
        try:
            self._store_source(tmp_path, stored)
        except Exception:
            if inserted:
                db.library_delete(h)
            raise

    @staticmethod
    def _store_source(tmp_path: Path, stored: Path) -> None:
        """Move an accepted upload into sources/ durably.

        sources/ is the only copy of the user's original — a staged WAV is
        re-derivable from it, it is not re-derivable from anything — and there
        is no battery any more, so the plug can come out at any instant. Sync
        the file, then the directory, so both the bytes and the name that
        reaches them survive a cut. Same shape as media.convert's final step.

        The order is the same as media.convert's final step: sync the temporary,
        rename it into place, then sync the directory.

        Path.replace rather than shutil.move: mkstemp writes into config.DATA
        and SOURCES is a subdirectory of it, so this is always a same-filesystem
        rename. shutil.move would fall back to a copy across filesystems, and
        that fallback can leave a partial file visible under the final name.

        Raises OSError if the bytes could not be confirmed on the card. The two
        fsyncs get different policies on purpose: the file's is the guarantee
        this method exists to make, so failing it has to reach the caller rather
        than let an upload be acknowledged for bytes that were never written.
        """
        # Sync before the rename, not after. sources/ is content-addressed, so
        # publishing the name first opens a window in which a power cut leaves
        # an unconfirmed file under that hash — and the next upload of the same
        # track would then find stored.exists(), skip the write entirely and
        # record a row against bytes that were never confirmed. Failing here
        # costs only the temporary, which nothing has referenced yet.
        try:
            with open(tmp_path, "rb") as f:
                os.fsync(f.fileno())
        except OSError:
            tmp_path.unlink(missing_ok=True)
            raise
        tmp_path.replace(stored)
        # The directory fsync only makes the *name* durable, and some
        # filesystems refuse it outright. The bytes are already safe by here,
        # and os.sync() on the session-end path covers the name, so a refusal
        # is not worth failing an upload over.
        try:
            dfd = os.open(str(stored.parent), os.O_RDONLY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError:
            log.warning("could not fsync %s", stored.parent, exc_info=True)

    def _source_for(self, h: str) -> Optional[Path]:
        for p in config.SOURCES.glob(f"{h}.*"):
            return p
        return None

    def _monitor(self) -> None:
        """Pedal detection."""
        while not self._stop.is_set():
            try:
                self._tick_pedal()
            except Exception as e:      # never let the monitor die
                log.exception("monitor tick failed")
                self.last_error = str(e)
            # Interruptible: shutdown must be able to stop the monitor before
            # it joins and unmounts, or a late tick could remount the pedal.
            self._stop.wait(config.POLL_SECS)

    def _tick_pedal(self) -> None:
        if self.ending or self._stop.is_set():
            return

        if not pedal.present():
            if self.pedal_state != "absent":
                self.pedal_state = "absent"
                self._loops = frozenset()   # presence is only knowable mounted
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

        # Run setup on any transition into the mounted state, including the
        # case where it was already mounted when we started — a service
        # restart mid-session must still probe the format and requeue.
        if self.pedal_state != "mounted":
            self.pedal_state = "mounted"
            self._mount_gen += 1        # a new session; stale loop jobs skip
            self.last_error = None
            pedal.clean_temp_files()
            self.fmt, self.fmt_source = pedal.detect_format()
            self._scan_loops()
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
        """Refresh the loop-presence cache from the pedal. Once per mount."""
        self._loops = frozenset(
            n for n in range(1, config.SLOTS + 1) if pedal.has_loop(n))

    @staticmethod
    def _purge_staged_loops() -> None:
        """Delete every transient staged loop copy. A staged file is disposable
        as long as the pedal holds the original, so this can run any time."""
        try:
            for p in config.LOOPS.glob("*"):
                try:
                    p.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    # ------------------------------------------------------------ work queue

    def _worker(self) -> None:
        job = None
        while not self._stop.is_set():
            # Don't start a job while an update is admitted: a redeploy/restart
            # must not overlap pedal work. Anything queued waits here and is
            # drained on the pending restart (or resumes if the update bails).
            if self._updating.is_set():
                self._stop.wait(0.1)
                continue
            if job is None:
                try:
                    job = self._work.get(timeout=0.5)
                except queue.Empty:
                    continue
                # Check again. An idle worker spends nearly all its time parked
                # in that get(), so a job arriving just after the gate closed
                # satisfies the get and would otherwise run straight past it —
                # which is the common case, not a narrow race.
                #
                # Hold the job rather than putting it back: the queue is FIFO
                # and requeuing would move it behind work queued later, and a
                # convert has to stay ahead of the write it queues. A held job
                # is invisible to update()'s idleness check, which is the safe
                # direction — it is not running, and a pending restart drops it
                # exactly like anything else still queued.
                if self._updating.is_set():
                    continue
            self._in_flight.set()
            try:
                self._run_job(job)
            except Exception as e:
                log.exception("job %r failed", job[0])
                self.last_error = str(e)
            finally:
                job = None
                self._in_flight.clear()
                self.busy = None
                self.busy_kind = None
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
        elif kind == "stage_loop":
            _, slot, gen, stage = job
            self._do_stage_loop(slot, gen, stage)
        elif kind == "delete_loop":
            _, slot, gen = job
            self._do_delete_loop(slot, gen)
        elif kind == "gc":
            self._gc()
        elif kind == "end":
            # A convert queues its write *after* the end marker, so the marker
            # can surface with real work still behind it. This worker is the
            # only thing draining the queue, so halting here would leave those
            # writes unwritten — go behind the remaining work instead.
            #
            # Collapse duplicate markers on the way past. end_session admits
            # under a lock so it can only ever queue one, but two would requeue
            # past each other forever and the device would never power off —
            # too sharp an edge to leave depending on a lock held elsewhere.
            pending = []
            while True:
                try:
                    pending.append(self._work.get_nowait())
                except queue.Empty:
                    break
            work = [j for j in pending if j[0] != "end"]
            for j in work:
                self._work.put(j)
            if work:
                self._work.put(("end",))
                return
            self._halt()

    def _do_convert(self, slot: int, h: str, src: Path) -> None:
        row = db.get_slot(slot)
        if not row or row["source_hash"] != h:
            return          # superseded by a newer upload
        self.busy = f"Converting {row['display_name']}"
        self.progress = 0.0
        self._emit()

        def prog(f):
            self.progress = f
            # ffmpeg emits a progress line several times a second and each
            # _emit rebuilds a full snapshot (two SQLite queries + a stat on a
            # Pi Zero), so cap it to ~5 Hz. The final state is emitted below.
            now = time.monotonic()
            if now - self._last_prog_emit >= 0.2:
                self._last_prog_emit = now
                self._emit()

        try:
            media.convert(src, h, self.fmt, progress=prog)
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

        wav = media.staged_path(row["source_hash"], self.fmt)
        if not wav.exists():
            src = self._source_for(row["source_hash"])
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
            rate = media.bytes_per_second(self.fmt)
            if rate:
                msg = f"won't fit — over capacity by {mmss((need - free) / rate)}"
            else:
                msg = "won't fit — not enough space on the pedal"
            db.set_state(slot, "error", msg)
            self._emit()
            return

        self.busy = f"Writing {row['display_name']}"
        self.busy_kind = "write"
        self.progress = 0.0
        self._emit()
        try:
            pedal.write_track(slot, wav)
        except OSError as e:
            db.set_state(slot, "error", f"write failed: {e}")
            self._emit()
            return
        os.sync()
        db.mark_synced(slot, row["source_hash"])
        self._emit()

    def _do_erase(self, slot: int) -> None:
        if not pedal.mounted():
            return
        if db.get_slot(slot):
            return          # slot was refilled before we got here
        # An unlink plus an os.sync is a write to the pedal like any other, and
        # the browser is now the only place a "don't unplug" warning can appear
        # — so this has to raise the flag even though it finishes quickly.
        self.busy = f"Clearing slot {slot:02d}"
        self.busy_kind = "write"
        self._emit()
        try:
            pedal.remove_track(slot)
            os.sync()
        except OSError as e:
            self.last_error = f"could not clear slot {slot}: {e}"
        self._emit()

    def _do_stage_loop(self, slot: int, gen: int, stage: LoopStage) -> None:
        """Copy the slot's loop off the pedal into a transient staging file.

        Stages to a path unique to this request, so concurrent downloads of the
        same slot never clobber each other's file. The staged path is handed to
        the waiter through `stage.publish()`, which loses to a concurrent
        cancel_and_reap() — so a request that times out mid-copy never leaves an
        orphan. Always sets `stage.done` (in the finally) so the waiter can never
        block past its own timeout, even if this raises. A read, so no os.sync().
        """
        dest = None
        try:
            if stage.cancelled():
                return                          # caller already gave up
            if gen != self._mount_gen or not pedal.mounted():
                stage.error = "no pedal"        # a different session, or none
                return
            if not pedal.has_loop(slot):
                # Vanished between the handler's has_loop() check and here.
                self._loops = self._loops - {slot}
                stage.error = "no loop"
                return
            dest = config.LOOPS / f"slot-{slot:02d}-{uuid.uuid4().hex}.wav"
            self.busy = "Reading loop"
            self.busy_kind = "read"
            self.progress = None
            self._emit()
            pedal.copy_loop(slot, dest)
            if not stage.publish(str(dest)):
                # The caller gave up while we copied; purge our own file so it
                # doesn't linger — no response generator will ever stream it.
                dest.unlink(missing_ok=True)
        except FileNotFoundError:
            if dest is not None:
                dest.unlink(missing_ok=True)
            self._loops = self._loops - {slot}  # the loop is gone; drop it
            stage.error = "no loop"
        except Exception as e:                  # surface like a failed convert
            if dest is not None:
                dest.unlink(missing_ok=True)
            stage.error = str(e)
        finally:
            stage.done.set()

    def _do_delete_loop(self, slot: int, gen: int) -> None:
        if gen != self._mount_gen or not pedal.mounted():
            return                              # a different session, or none
        if not pedal.has_loop(slot):
            self._loops = self._loops - {slot}
            self._emit()
            return
        self.busy = "Removing loop"
        self.busy_kind = "write"
        self._emit()
        try:
            pedal.remove_loop(slot)
            os.sync()
            self._loops = self._loops - {slot}
        except OSError as e:
            self.last_error = f"could not remove loop {slot}: {e}"
        self._emit()

    def _drain(self, timeout: float = 300.0) -> None:
        """Wait for the worker to go idle. Callers must not be the worker.

        `_in_flight` is set by the worker around every job, so a job that called
        this would be waiting on itself and could only ever time out. Only the
        SIGTERM path uses it, from the main thread.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            # busy alone is racy: the worker dequeues a job before it sets
            # busy, so check the in-flight flag too or a drain can return with
            # a write still running and unmount underneath it.
            if self._work.empty() and not self._in_flight.is_set():
                return
            time.sleep(0.2)

    def _halt(self) -> None:
        # No _drain here. This runs *as* the end job, inside the worker, so
        # `_in_flight` is already set on our own behalf — a drain could never
        # see idle and would burn its whole timeout before every poweroff. It
        # would also be redundant: the end marker requeues itself until the
        # queue is empty (see _run_job), which is the real wait.
        self.busy = "Finishing writes"
        self.busy_kind = "write"
        self._emit()
        self.busy = "Unmounting"
        self._emit()
        try:
            os.sync()
            pedal.unmount()
        except Exception as e:
            log.exception("unmount failed")
            self.last_error = str(e)
        self.busy = None
        self.busy_kind = None
        self._emit()

        self._gc()
        # The browser is the only status surface now, so give the last snapshot
        # a moment to reach it before the network goes away with the power.
        time.sleep(1.5)
        subprocess.run(["sudo", "-n", "/sbin/poweroff"], check=False)

    def _gc(self) -> None:
        """Prune expired trash and any file nothing references.

        sources/ and staged/ are collected under deliberately different rules,
        because they are different kinds of thing. A source *is* the library: it
        survives until the user deletes the track. A staged WAV is a cache,
        worth keeping only for a slot that is about to be written in the format
        the pedal currently wants — losing one costs a re-transcode, which
        _do_write requeues by itself. Keeping one per library track instead
        would be ruinous here: mono 24-bit at 44.1 kHz is 132 kB/s, so a
        four-minute track stages to ~32 MB, and a hundred of them would be ~3 GB
        of cache standing behind a few hundred MB of actual music.

        Runs at startup and at session end. The startup pass is the one that
        matters after a pulled plug, because _halt never ran.

        Recently-written files are left alone (see _older_than), so the pass at
        session end can leave a just-orphaned file behind. That costs a boot,
        not the space: the next startup pass collects it.
        """
        cutoff = time.time() - config.GC_GRACE_SECS
        try:
            db.prune_trash(config.TRASH_KEEP_DAYS)
            # .part files are transcodes interrupted by a crash or a pulled
            # plug — nothing ever references them, at any age.
            for f in config.STAGED.glob("*.wav.part"):
                f.unlink(missing_ok=True)
            # Keyed on the hash, not the full staged filename. The filename
            # carries a format tag, and self.fmt is still the default until the
            # monitor has mounted a pedal and probed it — but the startup
            # collector runs before that. Matching whole names would therefore
            # delete every WAV cached under the real format on every boot, and
            # cost a full re-transcode of all assigned slots. Bounded by the
            # slot count either way, so the format tag is left to decide which
            # staged file _do_write uses, not which ones survive.
            assigned = {r["source_hash"] for r in db.all_slots()}
            for f in config.STAGED.glob("*.wav"):
                h = f.name.split("-", 1)[0]
                if h not in assigned and self._older_than(f, cutoff):
                    f.unlink(missing_ok=True)
            for f in config.SOURCES.iterdir():
                if (f.is_file() and not db.hash_in_library(f.stem)
                        and self._older_than(f, cutoff)):
                    f.unlink(missing_ok=True)
            self._sweep_upload_temps()
        except Exception:
            log.exception("garbage collection failed")

    @staticmethod
    def _older_than(f: Path, cutoff: float) -> bool:
        """Is this file old enough to be safe to collect?

        upload() places the file in sources/ and only then writes the slot row,
        so a collector run in that window would see an unreferenced file and
        delete a upload that is moments from being referenced. Age is what tells
        the two apart. A file that vanished underneath us is not ours to worry
        about.
        """
        try:
            return f.stat().st_mtime < cutoff
        except OSError:
            return False

    @staticmethod
    def _sweep_upload_temps() -> None:
        """Delete stranded upload temporaries in DATA itself.

        The upload handlers mkstemp into config.DATA and hand the path to
        upload(), which moves or unlinks it. A crash or a pulled plug in
        between strands a file — potentially a few hundred MB of it — that
        nothing has ever swept.

        The age bound is what makes this safe to run at startup: an upload in
        flight right now owns its temp file, and only a stale one can be older
        than the whole-request upload path could plausibly take.
        """
        cutoff = time.time() - config.UPLOAD_TEMP_KEEP_SECS
        for f in config.DATA.glob("tmp*"):
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
            except OSError:
                pass
