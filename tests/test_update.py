"""Self-update: the POST /api/update status mapping (via a fake service) and the
core guard/redeploy branches that don't need a real git remote."""

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
