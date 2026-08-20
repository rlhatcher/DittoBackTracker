"""Self-update: the POST /api/update status mapping (via a fake service) and the
core guard/redeploy/update-check branches, using local git repos (no network)."""

import shutil
import subprocess
import threading

import pytest

from ditto import config, core, web, update


# --- endpoint status mapping ----------------------------------------------

class FakeService:
    def __init__(self, result):
        self._result = result

    def update(self):
        return self._result


def _client(result):
    app = web.create_app(FakeService(result))
    app.config.update(TESTING=True)
    return app.test_client()


def test_update_ok_200():
    rv = _client((True, "a1b2c3d")).post("/api/update")
    assert rv.status_code == 200
    j = rv.get_json()
    assert j["ok"] is True and j["revision"] == "a1b2c3d"


def test_update_busy_409():
    rv = _client((False, "busy — try again when the current work finishes")) \
        .post("/api/update")
    assert rv.status_code == 409


def test_update_already_running_409():
    rv = _client((False, "an update is already running")).post("/api/update")
    assert rv.status_code == 409


def test_update_failed_502():
    rv = _client((False, "git update failed: could not resolve host")) \
        .post("/api/update")
    assert rv.status_code == 502


def test_update_shutting_down_503():
    rv = _client((False, "the device is shutting down")).post("/api/update")
    assert rv.status_code == 503


def test_update_cross_site_403():
    rv = _client((True, "x")).post("/api/update",
                                   headers={"Origin": "http://evil.example"})
    assert rv.status_code == 403


class FakeCheckService:
    def __init__(self, payload):
        self._payload = payload

    def check_now(self):
        return self._payload


def _check_client(payload):
    app = web.create_app(FakeCheckService(payload))
    app.config.update(TESTING=True)
    return app.test_client()


def test_update_check_returns_payload():
    payload = {"ok": True, "error": None, "revision": "a1b2c3d",
               "update_available": True, "remote_revision": "e5f6a7b"}
    rv = _check_client(payload).post("/api/update/check")
    assert rv.status_code == 200
    assert rv.get_json() == payload


def test_update_check_cross_site_403():
    rv = _check_client({"ok": True}).post(
        "/api/update/check", headers={"Origin": "http://evil.example"})
    assert rv.status_code == 403


# --- core guards / redeploy -------------------------------------------------

@pytest.fixture
def service():
    svc = core.Service()
    # Startup queues a collector job, and update() refuses while any job is in
    # flight. These tests are about update()'s own guards, so wait for the
    # device to reach idle first rather than racing the boot sweep. _drain
    # returns silently when its timeout expires, so assert the device really
    # did settle — otherwise a slow sweep would have these tests assert on a
    # refusal that came from startup rather than from the guard under test.
    svc._drain(timeout=5.0)
    assert svc._work.empty() and not svc._in_flight.is_set(), \
        "startup work did not finish; these tests would assert on the wrong refusal"
    yield svc
    svc.shutdown(timeout=2.0)


def test_update_refused_while_busy(service):
    service.busy = "Writing something"
    ok, msg = service.update()
    assert ok is False and "busy" in msg


def test_update_refused_while_job_in_flight(service):
    # A job dequeued but not yet flagged busy still blocks admission, and the
    # updating gate is cleared again on the refusal.
    service._in_flight.set()
    try:
        ok, msg = service.update()
    finally:
        service._in_flight.clear()
    assert ok is False and "busy" in msg
    assert not service.updater.admitted.is_set()


def test_update_no_git_checkout(service, tmp_path, monkeypatch):
    # Idle, but SRC has no .git — the update must fail cleanly, not restart.
    monkeypatch.setattr(config, "SRC", tmp_path / "src")
    (tmp_path / "src").mkdir()
    ok, msg = service.update()
    assert ok is False and "no git checkout" in msg


def test_snapshot_has_version_fields(service):
    snap = service.snapshot()
    assert {"version", "revision", "update_available",
            "remote_revision"} <= snap.keys()
    assert snap["update_available"] is False   # no checkout in the temp data dir


# --- update availability check (local bare repo as "origin", no network) ----

def _git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True,
                   capture_output=True, text=True)


def _head(cwd):
    return subprocess.run(["git", "-C", str(cwd), "rev-parse", "HEAD"],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("git not available")
    origin, work, src = tmp_path / "origin", tmp_path / "work", tmp_path / "src"
    _git("init", "--bare", "-b", "main", str(origin))
    _git("clone", str(origin), str(work))
    _git("-C", str(work), "config", "user.email", "t@example.com")
    _git("-C", str(work), "config", "user.name", "Test")
    (work / "ditto").mkdir()
    (work / "ditto" / "__init__.py").write_text("# a\n")
    _git("-C", str(work), "add", "-A")
    _git("-C", str(work), "commit", "-m", "A")
    _git("-C", str(work), "push", "origin", "main")
    _git("clone", str(origin), str(src))
    return {"origin": origin, "work": work, "src": src}


def test_update_available_when_remote_ahead(service, repos, monkeypatch):
    src, work = repos["src"], repos["work"]
    monkeypatch.setattr(config, "SRC", src)
    monkeypatch.setattr(config, "UPDATE_BRANCH", "main")
    service.updater._current_sha = _head(src)
    service.updater.available, service.updater.remote_revision = False, None

    # Advance the remote past what the checkout has.
    (work / "ditto" / "__init__.py").write_text("# b\n")
    _git("-C", str(work), "commit", "-am", "B")
    _git("-C", str(work), "push", "origin", "main")

    service.updater._check_for_update()
    assert service.updater.available is True
    assert service.updater.remote_revision


def test_no_update_when_current(service, repos, monkeypatch):
    src = repos["src"]
    monkeypatch.setattr(config, "SRC", src)
    monkeypatch.setattr(config, "UPDATE_BRANCH", "main")
    service.updater._current_sha = _head(src)
    service.updater._check_for_update()
    assert service.updater.available is False
    assert service.updater.remote_revision is None      # null unless an update exists


def test_check_skipped_during_deploy(service, repos, monkeypatch):
    # A deploy holds _update_lock; the check must skip rather than run git on the
    # checkout concurrently.
    src = repos["src"]
    monkeypatch.setattr(config, "SRC", src)
    monkeypatch.setattr(config, "UPDATE_BRANCH", "main")
    service.updater._current_sha = _head(src)
    service.updater.available, service.updater.remote_revision = False, None
    assert service.updater._lock.acquire(blocking=False)
    try:
        service.updater._check_for_update()
    finally:
        service.updater._lock.release()
    assert service.updater.available is False
    assert service.updater.remote_revision is None      # skipped entirely


def test_startup_update_check_noops_without_checkout(service):
    # SRC has no checkout in the temp data dir, so the startup check is a fast
    # no-op that leaves the state untouched (and never raises).
    service.updater.startup_check()
    assert service.updater.available is False
    assert service.updater.remote_revision is None


def test_check_now_reports_available(service, repos, monkeypatch):
    src, work = repos["src"], repos["work"]
    monkeypatch.setattr(config, "SRC", src)
    monkeypatch.setattr(config, "UPDATE_BRANCH", "main")
    service.updater._current_sha = _head(src)

    (work / "ditto" / "__init__.py").write_text("# b\n")
    _git("-C", str(work), "commit", "-am", "B")
    _git("-C", str(work), "push", "origin", "main")

    res = service.check_now()
    assert res["ok"] is True
    assert res["update_available"] is True
    assert res["remote_revision"]


def test_check_now_without_checkout(service):
    # No checkout in the temp data dir → ok False with a reason, and no crash.
    res = service.check_now()
    assert res["ok"] is False and res["error"]
    assert res["update_available"] is False


def test_update_rolls_back_when_new_code_wont_load(service, repos, tmp_path,
                                                   monkeypatch):
    # The repo fixture's ditto/ is a stub with no web module, so the deployed
    # tree fails `import ditto.web` — the reset succeeds but the deploy must roll
    # back to the previous code and must NOT record a new revision.
    src = repos["src"]
    app = tmp_path / "app"
    (app / "ditto").mkdir(parents=True)
    (app / "ditto" / "__init__.py").write_text("# OLD deployed\n")
    monkeypatch.setattr(config, "SRC", src)
    monkeypatch.setattr(config, "APP", app)
    monkeypatch.setattr(config, "REVISION_FILE", app / "REVISION")
    monkeypatch.setattr(config, "UPDATE_BRANCH", "main")

    ok, msg = service.update()

    assert ok is False and "rolled back" in msg
    assert (app / "ditto" / "__init__.py").read_text() == "# OLD deployed\n"
    assert not (app / "REVISION").exists()      # no revision recorded on failure


def test_update_rolls_back_when_revision_unwritable(service, repos, tmp_path,
                                                    monkeypatch):
    # The swap and smoke-check succeed, but recording the revision fails; the
    # deploy must roll back rather than run code it can't record.
    src = repos["src"]
    app = tmp_path / "app"
    (app / "ditto").mkdir(parents=True)
    (app / "ditto" / "__init__.py").write_text("# OLD deployed\n")
    monkeypatch.setattr(config, "SRC", src)
    monkeypatch.setattr(config, "APP", app)
    monkeypatch.setattr(config, "UPDATE_BRANCH", "main")
    # A directory at the REVISION path makes the atomic replace fail.
    (app / "REVISION").mkdir()
    monkeypatch.setattr(config, "REVISION_FILE", app / "REVISION")
    # Pretend the deployed code imports cleanly.
    monkeypatch.setattr(update.Updater, "_import_check",
                        staticmethod(lambda app: None))

    ok, msg = service.update()

    assert ok is False and "could not record the revision" in msg
    assert (app / "ditto" / "__init__.py").read_text() == "# OLD deployed\n"


def test_a_successful_deploy_clears_the_remote_revision(service, repos,
                                                        tmp_path, monkeypatch):
    """remote_revision is only meaningful while an update is available.

    Leaving it set after a deploy makes the device report "up to date" next to
    a commit it supposedly needs. Observable whenever the process keeps running
    past the deploy — which is exactly what happens when the restart is
    refused, the case driven here.
    """
    src, work = repos["src"], repos["work"]
    monkeypatch.setattr(config, "SRC", src)
    monkeypatch.setattr(config, "APP", tmp_path / "app")
    monkeypatch.setattr(config, "REVISION_FILE", tmp_path / "app" / "REVISION")
    monkeypatch.setattr(config, "UPDATE_BRANCH", "main")
    (tmp_path / "app").mkdir()
    shutil.copytree(src / "ditto", tmp_path / "app" / "ditto")

    # Move the remote on, so a check finds an update and records its commit.
    (work / "ditto" / "__init__.py").write_text("# newer\n")
    _git("-C", str(work), "commit", "-am", "newer")
    _git("-C", str(work), "push", "origin", "main")
    service.updater._current_sha = _head(src)
    service.updater._check_for_update()
    assert service.updater.available is True
    assert service.updater.remote_revision is not None

    # Deploy for real, but refuse the restart so this process survives to be
    # asked. The smoke check is stubbed: the fixture repo is not a real package.
    monkeypatch.setattr(update.Updater, "_import_check",
                        staticmethod(lambda app: None))
    real = update.subprocess.run

    def no_systemd(cmd, **kw):
        if "systemctl" in " ".join(map(str, cmd)):
            raise OSError("restart refused")
        return real(cmd, **kw)

    monkeypatch.setattr(update.subprocess, "run", no_systemd)
    ok, msg = service.update()
    assert ok is False and "restart was refused" in msg

    snap = service.snapshot()
    assert snap["update_available"] is False
    assert snap["remote_revision"] is None, \
        "reported up to date while still naming a commit to update to"


def test_the_gate_is_cleared_before_the_lock_is_released(service, monkeypatch):
    """A failed update must not reopen the gate under a second one.

    Releasing the lock first lets another update acquire it and set the gate;
    this one's clear would then reopen it with that deploy already running,
    leaving the worker free to touch the pedal during a git reset and a
    restart. The window is a couple of bytecodes, so assert the ordering
    rather than racing it.
    """
    order = []
    upd = service.updater

    class RecordingLock:
        """A stand-in for the lock itself — _thread.lock's methods are
        read-only, so the attribute is replaced rather than patched."""

        def __init__(self):
            self._real = threading.Lock()

        def acquire(self, blocking=True):
            return self._real.acquire(blocking)

        def release(self):
            order.append("release")
            self._real.release()

    monkeypatch.setattr(upd, "_lock", RecordingLock())
    real_clear = upd.admitted.clear
    monkeypatch.setattr(upd.admitted, "clear",
                        lambda: (order.append("clear"), real_clear())[1])

    service.busy = "Writing something"          # forces the refusal path
    ok, _ = service.update()

    assert ok is False
    assert order == ["clear", "release"], (
        f"the gate outlived the lock: {order}")
