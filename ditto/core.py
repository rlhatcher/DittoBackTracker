"""The service: pedal lifecycle, transcode queue, write queue, panel."""

from __future__ import annotations

import logging
import os
import queue
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from . import config, db, media, pedal
from .panel import Panel, local_ip
from .power import Battery

log = logging.getLogger(__name__)


def mmss(seconds: float) -> str:
    m, s = divmod(int(max(seconds, 0)), 60)
    return f"{m}:{s:02d}"


class Service:
    def __init__(self, headless: bool = False) -> None:
        config.ensure_dirs()
        self.battery = Battery()
        self.panel = None
        if not headless:
            self.battery.start()
            self.panel = Panel(on_press=self.end_session,
                               on_long=self.force_halt)

        self._work: "queue.Queue[tuple]" = queue.Queue()
        self._subs: List["queue.Queue[dict]"] = []
        self._subs_lock = threading.Lock()

        self.fmt: Dict = dict(config.DEFAULT_FORMAT)
        self.fmt_source = "default"
        self.pedal_state = "absent"          # absent | mounted | error
        self.busy: Optional[str] = None      # human-readable current activity
        self.progress: Optional[float] = None
        self.last_error: Optional[str] = None
        self.ending = False

        self._stop = threading.Event()
        self._threads = [
            threading.Thread(target=self._worker, daemon=True, name="worker"),
            threading.Thread(target=self._monitor, daemon=True, name="monitor"),
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
        snap = self.snapshot()
        with self._subs_lock:
            for q in list(self._subs):
                try:
                    q.put_nowait(snap)
                except queue.Full:
                    pass

    # ---------------------------------------------------------------- state

    def capacity(self) -> Dict:
        free, total = pedal.capacity()
        rate = media.bytes_per_second(self.fmt)
        used_secs = sum(s["duration"] for s in db.all_slots())
        if total and rate:
            total_secs = total / rate
        else:
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
            "pedal": self.pedal_state,
            "busy": self.busy,
            "progress": self.progress,
            "ending": self.ending,
            "error": self.last_error,
            "format": self.fmt,
            "format_source": self.fmt_source,
            "slots": db.all_slots(),
            "slot_count": config.SLOTS,
            "capacity": self.capacity(),
            "battery": {
                "available": self.battery.available,
                "percent": self.battery.percent,
                "charging": self.battery.charging,
                "can_write": self.battery.can_write(),
            },
            "panel": self.panel.status() if self.panel else {},
            "ip": local_ip(),
        }

    # ------------------------------------------------------------ operations

    @staticmethod
    def _check_slot(slot: int) -> None:
        if not (1 <= slot <= config.SLOTS):
            raise ValueError(f"slot must be 1-{config.SLOTS}")

    def upload(self, slot: int, tmp_path: Path, display_name: str) -> Dict:
        self._check_slot(slot)

        info = media.probe(tmp_path)
        if info is None or info.duration <= 0:
            tmp_path.unlink(missing_ok=True)
            raise ValueError("not a readable audio file")

        h = media.file_hash(tmp_path)
        # _source_for globs for "{hash}.*", so an extensionless upload would be
        # stored under a name it can never find again.
        stored = config.SOURCES / f"{h}{tmp_path.suffix.lower() or '.bin'}"
        if not stored.exists():
            shutil.move(str(tmp_path), str(stored))
        else:
            tmp_path.unlink(missing_ok=True)

        existing = db.get_slot(slot)
        if existing:
            db.delete_slot(slot, to_trash=True)

        db.put_slot(slot, h, display_name, info.duration, state="converting")
        self._work.put(("convert", slot, h, stored))
        self._emit()
        return db.get_slot(slot)

    def clear(self, slot: int) -> Optional[int]:
        self._check_slot(slot)
        trash_id = db.delete_slot(slot, to_trash=True)
        self._work.put(("erase", slot))
        self._emit()
        return trash_id

    def move(self, src: int, dst: int) -> None:
        """Move to an empty slot, or swap with an occupied one.

        Swapping rather than overwriting means reordering a setlist never
        destroys anything, so no trash entry and no undo needed.
        """
        self._check_slot(src)
        self._check_slot(dst)
        if src == dst or not db.get_slot(src):
            return
        if db.get_slot(dst):
            db.swap_slots(src, dst)
            self._work.put(("write", src))
            self._work.put(("write", dst))
        else:
            db.move_slot(src, dst)
            self._work.put(("erase", src))
            self._work.put(("write", dst))
        self._emit()

    def retry(self, slot: int) -> None:
        self._check_slot(slot)
        row = db.get_slot(slot)
        if not row:
            return
        src = self._source_for(row["source_hash"])
        if not src:
            db.set_state(slot, "error", "source file missing")
            self._emit()
            return
        db.set_state(slot, "converting")
        self._work.put(("convert", slot, row["source_hash"], src))
        self._emit()

    def restore(self, trash_id: int) -> Optional[int]:
        item = db.trash_pop(trash_id)
        if not item:
            return None
        slot = item["slot"]
        if db.get_slot(slot):
            db.delete_slot(slot, to_trash=True)
        db.put_slot(slot, item["source_hash"], item["display_name"],
                    item["duration"], state="converting")
        src = self._source_for(item["source_hash"])
        if src:
            self._work.put(("convert", slot, item["source_hash"], src))
        else:
            db.set_state(slot, "error", "source file missing")
        self._emit()
        return slot

    def end_session(self) -> None:
        """Flush, unmount, halt. The graceful ending."""
        if self.ending:
            return
        self.ending = True
        self._emit()
        self._work.put(("end",))

    def force_halt(self) -> None:
        """Long press. Emergency only — does not wait for the queue."""
        if self.ending:
            return
        self.ending = True
        self._emit()
        threading.Thread(target=self._halt, args=(False,), daemon=True).start()

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
        if self.panel:
            self.panel.close()
        self.battery.close()

    # -------------------------------------------------------------- internals

    def _source_for(self, h: str) -> Optional[Path]:
        for p in config.SOURCES.glob(f"{h}.*"):
            return p
        return None

    def _monitor(self) -> None:
        """Pedal detection and panel rendering."""
        while not self._stop.is_set():
            try:
                self._tick_pedal()
                self._render()
            except Exception as e:      # never let the monitor die
                log.exception("monitor tick failed")
                self.last_error = str(e)
            time.sleep(config.POLL_SECS)

    def _tick_pedal(self) -> None:
        if self.ending:
            return

        if not pedal.present():
            if self.pedal_state != "absent":
                self.pedal_state = "absent"
                self._emit()
            return

        try:
            pedal.mount()               # no-op if already mounted
        except pedal.PedalError as e:
            if self.pedal_state != "error":
                self.pedal_state = "error"
                self.last_error = str(e)
                self._emit()
            return

        # Run setup on any transition into the mounted state, including the
        # case where it was already mounted when we started — a service
        # restart mid-session must still probe the format and requeue.
        if self.pedal_state != "mounted":
            self.pedal_state = "mounted"
            self.last_error = None
            pedal.clean_temp_files()
            self.fmt, self.fmt_source = pedal.detect_format()
            self._requeue_unsynced()
            self._emit()

        if self.battery.critical() and not self.ending:
            self.last_error = "battery critical — ending session"
            self.end_session()

    def _requeue_unsynced(self) -> None:
        on_pedal = pedal.occupied_slots()
        for row in db.all_slots():
            slot, h = row["slot"], row["source_hash"]
            if row["state"] == "error":
                continue
            if row["synced_hash"] != h or slot not in on_pedal:
                self._work.put(("write", slot))

    def _render(self) -> None:
        if not self.panel:
            return
        if self.ending:
            return
        bat = self.battery.percent
        bat_s = f"{bat}%" if bat is not None else "--"

        if self.busy:
            lines = [f"{self.busy}"[:21]]
            slots = db.all_slots()
            done = sum(1 for s in slots if s["state"] == "synced")
            pos = min(done + 1, len(slots)) if slots else 0
            lines.append(f"Slot {pos} of {len(slots)}   {bat_s}")
            lines.append("WRITING - DONT PULL")
            self.panel.render("writing", lines, self.progress)
        elif self.pedal_state == "mounted":
            cap = self.capacity()
            self.panel.render("idle", [
                f"{local_ip()}"[:21],
                f"{cap['used_label']} of {cap['total_label']}  {bat_s}",
                "Ready - press to end",
            ])
        elif self.last_error:
            self.panel.render("error", ["Error:", self.last_error[:21]])
        else:
            self.panel.render("boot", [
                f"{local_ip()}"[:21],
                f"Battery {bat_s}",
                "Plug in the pedal",
            ])

    # ------------------------------------------------------------ work queue

    def _worker(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._work.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._run_job(job)
            except Exception as e:
                log.exception("job %r failed", job[0])
                self.last_error = str(e)
            finally:
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
        elif kind == "end":
            # A convert queues its write *after* the end marker, so the marker
            # can surface with real work still behind it. This worker is the
            # only thing draining the queue, so halting here would leave those
            # writes unwritten — requeue and keep going instead.
            if not self._work.empty():
                self._work.put(("end",))
                return
            self._halt(True)

    def _do_convert(self, slot: int, h: str, src: Path) -> None:
        row = db.get_slot(slot)
        if not row or row["source_hash"] != h:
            return          # superseded by a newer upload
        self.busy = f"Converting {row['display_name']}"
        self.progress = 0.0
        self._emit()

        def prog(f):
            self.progress = f
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
        if not self.battery.can_write():
            db.set_state(slot, "staged", "battery too low to write")
            self._emit()
            return

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
            over = (need - free) / media.bytes_per_second(self.fmt)
            db.set_state(slot, "error",
                         f"won't fit — over capacity by {mmss(over)}")
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
        os.sync()
        db.mark_synced(slot, row["source_hash"])
        self._emit()

    def _do_erase(self, slot: int) -> None:
        if not pedal.mounted():
            return
        if db.get_slot(slot):
            return          # slot was refilled before we got here
        try:
            pedal.remove_track(slot)
            os.sync()
        except OSError as e:
            self.last_error = f"could not clear slot {slot}: {e}"
        self._emit()

    def _drain(self, timeout: float = 300.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._work.empty() and self.busy is None:
                return
            time.sleep(0.2)

    def _halt(self, graceful: bool) -> None:
        if graceful:
            self.busy = "Finishing writes"
            self._emit()
            # The end marker only reaches _halt once the queue is empty, so
            # this is a short safety bound rather than the actual wait.
            self._drain(timeout=5.0)
        self.busy = "Unmounting"
        self._emit()
        try:
            os.sync()
            pedal.unmount()
        except Exception as e:
            log.exception("unmount failed")
            self.last_error = str(e)
        self.busy = None
        self._emit()

        self._gc()
        if self.panel:
            self.panel.shutdown_message()
        time.sleep(1.5)
        if self.panel:
            self.panel.close()
        subprocess.run(["sudo", "-n", "/sbin/poweroff"], check=False)

    def _gc(self) -> None:
        """Prune expired trash and any staged file nothing references."""
        try:
            db.prune_trash(config.TRASH_KEEP_DAYS)
            # .part files are transcodes interrupted by a crash or a pulled
            # plug — nothing ever references them.
            for f in config.STAGED.glob("*.wav.part"):
                f.unlink(missing_ok=True)
            for f in config.STAGED.glob("*.wav"):
                h = f.name.split("-", 1)[0]
                if not db.hash_in_use(h):
                    f.unlink(missing_ok=True)
            for f in config.SOURCES.iterdir():
                if f.is_file() and not db.hash_in_use(f.stem):
                    f.unlink(missing_ok=True)
        except Exception:
            log.exception("garbage collection failed")
