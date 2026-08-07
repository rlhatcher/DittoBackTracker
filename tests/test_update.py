"""Self-update: the POST /api/update status mapping (via a fake service) and the
core guard/redeploy/update-check branches, using local git repos (no network)."""

import shutil
import subprocess

import pytest

from ditto import config, core, web


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


# --- core guards / redeploy -------------------------------------------------

@pytest.fixture
def service():
    svc = core.Service(headless=True)
    yield svc
    svc.shutdown(timeout=2.0)


def test_update_refused_while_busy(service):
    service.busy = "Writing something"
    ok, msg = service.update()
    assert ok is False and "busy" in msg


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
    service._current_sha = _head(src)
    service._update_available, service._remote_revision = False, None

    # Advance the remote past what the checkout has.
    (work / "ditto" / "__init__.py").write_text("# b\n")
    _git("-C", str(work), "commit", "-am", "B")
    _git("-C", str(work), "push", "origin", "main")

    service._check_for_update()
    assert service._update_available is True
    assert service._remote_revision


def test_no_update_when_current(service, repos, monkeypatch):
    src = repos["src"]
    monkeypatch.setattr(config, "SRC", src)
    monkeypatch.setattr(config, "UPDATE_BRANCH", "main")
    service._current_sha = _head(src)
    service._check_for_update()
    assert service._update_available is False


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
