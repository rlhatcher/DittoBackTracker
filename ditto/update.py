"""Over-the-air self-update: pull the tracked branch, redeploy, restart.

Kept apart from core.py because it shares nothing with the rest of the service
beyond the worker's job lock, which it holds for the deploy so a restart never
overlaps pedal work.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Dict, Optional

from . import config

log = logging.getLogger(__name__)


class Updater:
    def __init__(self, job_lock: threading.Lock,
                 is_busy: Callable[[], bool],
                 stopped: Callable[[], bool],
                 on_change: Callable[[], None]) -> None:
        self._job_lock = job_lock
        self._is_busy = is_busy
        self._stopped = stopped
        self._changed = on_change
        # Serializes updates and checks, so git never runs twice on the
        # checkout at once.
        self._lock = threading.Lock()
        # The deployed commit is the checkout's HEAD: a deploy resets the
        # checkout and copies from it in one step.
        self._current_sha = self._rev_parse(config.SRC, "HEAD")
        self.revision = self._current_sha[:7] if self._current_sha else None
        self.available = False
        self.remote_revision: Optional[str] = None

    def update(self) -> "tuple[bool, str]":
        """Pull the tracked branch, redeploy, and restart out of process.

        Returns (ok, message): the deployed short commit, or the reason. Runs
        holding the job lock; on success the lock is kept, because a restart is
        pending and the worker must not touch the pedal under it.
        """
        if not self._lock.acquire(blocking=False):
            return (False, "an update is already running")
        try:
            busy = (False, "busy — try again when the current work finishes")
            if not self._job_lock.acquire(blocking=False):
                return busy
            result = (False, "update failed")
            try:
                result = busy if self._is_busy() else self._do_update()
            finally:
                if not result[0]:
                    self._job_lock.release()
            return result
        finally:
            self._lock.release()

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
        target = self._rev_parse(src, "HEAD")

        # Build the new tree beside the live one and swap by rename, keeping
        # the old one as ditto.bak for the rollback below.
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
                bak.rename(live)
            shutil.rmtree(new, ignore_errors=True)
            return (False, f"deploy failed: {e}")

        # Import the deployed package before committing to a restart, which
        # cannot be observed from the process being restarted.
        check = self._import_check(app)
        if check is not None:
            err = self._rollback(live, bak)
            if err:
                return (False, f"new code failed to load and rollback failed "
                               f"({err}); manual recovery may be needed")
            return (False, f"new code failed to load; rolled back: {check}")

        self._current_sha = target
        self.revision = target[:7] if target else None
        self.available = False
        self.remote_revision = None
        try:
            # Absolute path, to match the sudoers rule exactly.
            subprocess.run(
                ["sudo", "-n", "/usr/bin/systemctl", "start", "--no-block",
                 config.RESTART_SERVICE],
                check=True, capture_output=True, text=True, timeout=15)
        except (subprocess.SubprocessError, OSError) as e:
            return (False, f"deployed {self.revision or 'update'} but the "
                           f"restart was refused — is OTA set up? "
                           f"{self._proc_err(e)}")
        return (True, self.revision or "updated")

    @staticmethod
    def _rollback(live: Path, bak: Path) -> Optional[str]:
        """Put ditto.bak back. None on success, else a short error."""
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
        try:
            r = subprocess.run([sys.executable, "-c", "import ditto.web"],
                               cwd=str(app), capture_output=True, text=True,
                               timeout=30)
        except (subprocess.SubprocessError, OSError) as e:
            return str(e)[:200]
        if r.returncode != 0:
            return (r.stderr.strip() or "import failed").splitlines()[-1][:200]
        return None

    def startup_check(self) -> None:
        self._check_for_update()

    def check_now(self) -> Dict:
        err = self._check_for_update()
        return {
            "ok": err is None,
            "error": err,
            "revision": self.revision,
            "update_available": self.available,
            "remote_revision": self.remote_revision,
        }

    def _check_for_update(self) -> Optional[str]:
        """Fetch the remote and flag whether it differs from the deployed
        commit. None when the check ran, else a short reason."""
        src = config.SRC
        if self._current_sha is None or not (src / ".git").is_dir():
            return "no deployment to check"
        if not self._lock.acquire(blocking=False):
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
            remote_short = self._rev_parse(src, ref, short=True) if available else None
            if self._stopped():
                return None
            if (available != self.available
                    or remote_short != self.remote_revision):
                self.available = available
                self.remote_revision = remote_short
                self._changed()
        finally:
            self._lock.release()
        return None

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
