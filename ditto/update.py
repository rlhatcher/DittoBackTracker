"""Over-the-air self-update: pull the tracked branch, redeploy, restart.

Kept apart from core.py because it shares nothing with the rest of the service.
It talks to git and systemd — a third external system alongside the pedal and
ffmpeg — and it owns its own state entirely. Its only contact with the session
is three questions it has to ask before it may run, which arrive as callables:
is the device shutting down, is it busy, and has it been told to stop.

The one piece of shared machinery is `admitted`. The worker checks it before
starting any job, so a redeploy never overlaps pedal work; see Service._worker
for the ordering that makes the two mutually exclusive.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Dict, Optional

from . import config

log = logging.getLogger(__name__)


class Updater:
    def __init__(self, is_ending: Callable[[], bool],
                 is_busy: Callable[[], bool],
                 stopped: Callable[[], bool],
                 on_change: Callable[[], None]) -> None:
        self._is_ending = is_ending
        self._is_busy = is_busy
        self._stopped = stopped
        self._changed = on_change

        # Serializes self-update so two clicks can't redeploy on top of each
        # other. Held only for the brief git-pull + redeploy, never a job.
        self._lock = threading.Lock()
        # Set while an update is admitted: gates the worker from starting any
        # new job, so a redeploy/restart never overlaps pedal work. Stays set
        # once a restart is pending; cleared if the update bails out.
        self.admitted = threading.Event()

        # Deployed code identity + whether the remote has something newer.
        # `revision`/`_current_sha` are the SHA actually deployed (recorded at
        # the last successful deploy, else the checkout HEAD) — cheap, no
        # network — read once at startup; `available`/`remote_revision` are set
        # by the startup check and by on-demand checks the user triggers.
        self.revision, self._current_sha = self._deployed_head()
        self.available = False
        self.remote_revision: Optional[str] = None

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
        if self._is_ending():
            return (False, "the device is shutting down")
        if not self._lock.acquire(blocking=False):
            return (False, "an update is already running")
        # Gate the worker before inspecting idleness, so a job can't slip from
        # the queue into flight between the check and the deploy.
        self.admitted.set()
        result = (False, "update failed")
        try:
            if self._is_busy():
                result = (False, "busy — try again when the current work finishes")
            else:
                result = self._do_update()
        finally:
            # Clear before releasing, never after. Releasing first lets a second
            # update take the lock and set the gate, and this clear would then
            # reopen it with that deploy already running — the worker free to
            # touch the pedal during a git reset and a restart, which is the one
            # thing the gate exists to prevent.
            if not result[0]:
                self.admitted.clear()      # no restart pending — resume work
            self._lock.release()
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
        self.revision = target[:7] if target else None
        # Both, together: remote_revision is only meaningful while an update is
        # available, and _check_for_update maintains that pairing. Clearing one
        # without the other leaves the device reporting "up to date" alongside
        # a commit it supposedly needs — visible whenever the process keeps
        # running past a deploy, which is what happens when the restart is
        # refused.
        self.available = False
        self.remote_revision = None

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
            return (False, f"deployed {self.revision or 'update'} but the "
                           f"restart was refused — is OTA set up? "
                           f"{self._proc_err(e)}")
        return (True, self.revision or "updated")

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

    def startup_check(self) -> None:
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
            "revision": self.revision,
            "update_available": self.available,
            "remote_revision": self.remote_revision,
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
            # Contract: remote_revision is meaningful only when an update is
            # available; keep it null otherwise.
            remote_short = self._rev_parse(src, ref, short=True) if available else None
            # Publish under the lock: a deploy holds the same lock, so this can't
            # overwrite update_available against a _current_sha the deploy has
            # since moved. Skip during teardown.
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
