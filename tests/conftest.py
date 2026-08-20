"""Shared fixtures, and the two pieces of setup that have to run at import.

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
from ditto import config, core, db, update, web       # noqa: E402


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


_block_sudo()


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
        svc._drain(timeout=5.0)
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
