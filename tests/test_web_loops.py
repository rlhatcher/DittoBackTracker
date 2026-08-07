"""The two loop endpoints, driven through a Flask test client against a
duck-typed fake Service — no worker threads, no hardware, no real pedal."""

import pytest

from ditto import config, core, pedal, web


class FakeService:
    """Stands in for core.Service. Only the methods the loop routes call."""

    def __init__(self, tmp, loops=frozenset()):
        self._tmp = tmp
        self._loops = set(loops)

    @staticmethod
    def _check_slot(slot):
        if not (1 <= slot <= config.SLOTS):
            raise ValueError(f"slot must be 1-{config.SLOTS}")

    def has_loop(self, slot):
        return slot in self._loops

    def stage_loop(self, slot):
        # Emulate the worker: write a staged file, publish it, signal done.
        stage = core.LoopStage()
        p = self._tmp / f"slot-{slot:02d}.wav"
        p.write_bytes(b"LOOPBYTES")
        stage.publish(str(p))
        stage.done.set()
        return stage

    def delete_loop(self, slot):
        if slot not in self._loops:
            return False
        self._loops.discard(slot)
        return True


@pytest.fixture
def env(tmp_path, monkeypatch):
    svc = FakeService(tmp_path, loops={5})
    monkeypatch.setattr(pedal, "mounted", lambda: True)
    app = web.create_app(svc)
    app.config.update(TESTING=True)
    return app.test_client(), svc, tmp_path


def test_get_no_pedal_503(env, monkeypatch):
    client, _, _ = env
    monkeypatch.setattr(pedal, "mounted", lambda: False)
    assert client.get("/api/loops/5").status_code == 503


def test_get_no_loop_404(env):
    client, _, _ = env
    assert client.get("/api/loops/9").status_code == 404


def test_get_streams_and_purges(env):
    client, _, tmp = env
    rv = client.get("/api/loops/5")
    assert rv.status_code == 200
    assert rv.data == b"LOOPBYTES"
    assert "attachment" in rv.headers["Content-Disposition"]
    assert "loop-05.wav" in rv.headers["Content-Disposition"]
    rv.close()
    # The transient staged copy is purged once the response completes.
    assert not (tmp / "slot-05.wav").exists()


def test_get_timeout_503(env, monkeypatch):
    client, svc, _ = env
    monkeypatch.setattr(config, "LOOP_STAGE_TIMEOUT", 0.05)

    # A stage that never signals completion: the handler must time out and 503.
    def never_done(slot):
        return core.LoopStage()             # done never set

    svc.stage_loop = never_done
    assert client.get("/api/loops/5").status_code == 503


def test_delete_no_loop_404(env):
    client, _, _ = env
    assert client.delete("/api/loops/9").status_code == 404


def test_delete_ok(env):
    client, svc, _ = env
    rv = client.delete("/api/loops/5")
    assert rv.status_code == 200
    assert rv.get_json()["ok"] is True
    assert not svc.has_loop(5)


def test_delete_cross_site_rejected(env):
    client, _, _ = env
    rv = client.delete("/api/loops/5", headers={"Origin": "http://evil.example"})
    assert rv.status_code == 403


def test_loopstage_worker_wins():
    # Worker publishes before the waiter gives up: the waiter reaps the path.
    stage = core.LoopStage()
    assert stage.publish("/tmp/x.wav") is True
    assert stage.cancel_and_reap() == "/tmp/x.wav"   # waiter must purge it


def test_loopstage_waiter_wins():
    # Waiter gives up first: a later publish is refused, so the worker purges.
    stage = core.LoopStage()
    assert stage.cancel_and_reap() is None
    assert stage.cancelled() is True
    assert stage.publish("/tmp/x.wav") is False
