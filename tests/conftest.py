"""Shared fixtures, and the setup that has to run at import.

Point the app's data/mount paths at throwaway temp dirs before anything in the
ditto package imports config (which resolves them at import time). This keeps a
test run from touching /var/lib/ditto or /media/ditto — neither of which exists
or is writable on CI.
"""

import os
import subprocess
import tempfile

import pytest

_tmp = tempfile.mkdtemp(prefix="ditto-test-")
# Unconditional, not setdefault: a dev shell that exports a real DITTO_MOUNT
# would otherwise let the filesystem tests (test_pedal calls remove_loop) act on
# a real pedal. Force throwaway temp paths for every run.
os.environ["DITTO_DATA"] = os.path.join(_tmp, "data")
os.environ["DITTO_MOUNT"] = os.path.join(_tmp, "mount")

# Imported after the environment is set, or config resolves the real paths.
from ditto import config, core, db, media, update, web  # noqa: E402

# Every sudo the app runs, recorded instead of executed. Read it in a test if
# you want to assert one was attempted.
sudo_attempts: list[list[str]] = []


def _block_sudo() -> None:
    """Stop the suite from powering off the machine it is running on.

    _halt ends with `sudo -n /sbin/poweroff`, and install.sh grants the ditto
    user a NOPASSWD rule for exactly that command. So a test that reaches _halt
    — by leaving an end marker queued, say — powers off a provisioned device
    part-way through the run. On a laptop the sudo call simply fails, which is
    why this stayed invisible.

    Replaced at import rather than per-test, because the reach can happen during
    fixture teardown, after a test's own monkeypatches have been undone. A test
    that wants the real call recorded can still stub subprocess.run itself.
    """
    for mod in (core, update):
        real = mod.subprocess.run

        def guard(*args, _real=real, **kwargs):
            cmd = args[0] if args else kwargs.get("args", [])
            if isinstance(cmd, (list, tuple)) and cmd and str(cmd[0]) == "sudo":
                sudo_attempts.append([str(c) for c in cmd])
                return subprocess.CompletedProcess(list(cmd), 0, "", "")
            return _real(*args, **kwargs)

        mod.subprocess.run = guard


def _block_ffmpeg() -> None:
    """Stop the suite from ever reaching a real ffmpeg or ffprobe.

    test_media.py's docstring states that neither is invoked and that every
    crossing is stubbed. Nothing enforced it. A test that forgets to stub
    passes on a laptop with ffmpeg installed, and CI has no ffmpeg, so the same
    test fails there for a reason that reads like a broken image rather than a
    broken test.

    Raises rather than returning a plausible empty result. A test that reaches
    the real binary has a bug in the test, and a fake CompletedProcess would
    hide it behind an assertion about an empty stream list.

    Wraps media's module reference, which is the only place either is named.
    """
    real_run, real_popen = media.subprocess.run, media.subprocess.Popen

    def _blocked(cmd) -> str:
        head = str(cmd[0]) if isinstance(cmd, (list, tuple)) and cmd else ""
        return head if head in ("ffmpeg", "ffprobe") else ""

    def guard_run(*args, **kwargs):
        name = _blocked(args[0] if args else kwargs.get("args", []))
        if name:
            raise AssertionError(f"the suite tried to run {name}; stub it "
                                 "instead (see test_media.py)")
        return real_run(*args, **kwargs)

    def guard_popen(*args, **kwargs):
        name = _blocked(args[0] if args else kwargs.get("args", []))
        if name:
            raise AssertionError(f"the suite tried to run {name}; stub it "
                                 "instead (see test_media.py)")
        return real_popen(*args, **kwargs)

    media.subprocess.run = guard_run
    media.subprocess.Popen = guard_popen


_block_sudo()
_block_ffmpeg()


def _forget_connection():
    """Drop this thread's cached SQLite handle.

    db.conn() caches one connection per (thread, path). A test that repoints
    DB_PATH and does not clear this would keep talking to whichever database
    the thread opened first, and leave the next test doing the same.
    """
    for attr in ("conn", "path"):
        if hasattr(db._local, attr):
            delattr(db._local, attr)


@pytest.fixture
def data_tree(tmp_path, monkeypatch):
    """A private data directory: its own database and its own file tree.

    Every path is repointed individually. They are derived from DATA when config
    is imported, so patching DATA alone would leave the rest aimed at the shared
    tree — which is how tests end up quietly sharing state.
    """
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "state.db")
    for name in ("SOURCES", "STAGED", "TRASH", "LOOPS"):
        d = tmp_path / name.lower()
        monkeypatch.setattr(config, name, d)
        d.mkdir(parents=True, exist_ok=True)
    _forget_connection()
    yield tmp_path
    _forget_connection()


# Service._drain returns silently when it gives up, so a timeout that is too
# short does not fail as a timeout. It fails later, on whatever the test was
# about to assert, with a message that blames the wrong thing: CircleCI build
# 140 reported "ending the session never reached poweroff" when the poweroff
# was merely still coming.
#
# The end job is the slowest thing a test waits for. It sleeps 1.5 s on purpose
# (core._halt, so the last snapshot reaches the browser before the power goes)
# and calls os.sync(), which flushes every filesystem on the host and is
# unbounded on a shared runner. 5 s left about 3.5 s for that. Local runs use
# 1.6 s of it, which looked like room and was not.
#
# This is not a deadline anything is measured against. _drain polls at 50 ms and
# returns the moment the worker is idle, so a generous timeout costs a healthy
# run nothing and only bounds a hang.
DRAIN_TIMEOUT = 60.0


def drain(svc, timeout: float = DRAIN_TIMEOUT) -> None:
    """Wait for the worker to go idle, and say so when it doesn't."""
    svc._drain(timeout=timeout)
    assert svc._work.empty() and not svc._in_flight.is_set(), (
        f"worker still busy after {timeout}s: "
        f"queued={not svc._work.empty()} in_flight={svc._in_flight.is_set()}")


@pytest.fixture
def service(data_tree):
    """A running Service on its own data tree.

    The drain lets the boot sweep finish, so a test about a guard is not left
    asserting on a refusal that came from the collector. try/finally because a
    failure before the yield would otherwise leave the worker and monitor
    threads running into the next test.
    """
    svc = core.Service()
    try:
        drain(svc)
        yield svc
    finally:
        svc.shutdown(timeout=2.0)


@pytest.fixture
def app(service):
    return web.create_app(service)


@pytest.fixture
def client(app):
    app.config.update(TESTING=True)
    return app.test_client()
