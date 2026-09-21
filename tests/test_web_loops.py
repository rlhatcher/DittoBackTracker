"""The two loop routes, against a duck-typed fake Service: no worker, no
hardware."""

import pytest

from ditto import config, web


class FakeService:
    def __init__(self, tmp, loops=frozenset()):
        self._tmp = tmp
        self._loops = set(loops)
        self.mounted = True

    @staticmethod
    def check_slot(slot):
        if not (1 <= slot <= config.SLOTS):
            raise ValueError(f"slot must be 1-{config.SLOTS}")

    def has_loop(self, slot):
        return slot in self._loops

    def loop_path(self, slot):
        p = self._tmp / f"slot-{slot:02d}.wav"
        if slot in self._loops:
            p.write_bytes(b"LOOPBYTES")
        return p

    def delete_loop(self, slot):
        if slot not in self._loops:
            return False
        self._loops.discard(slot)
        return True


@pytest.fixture
def env(tmp_path):
    svc = FakeService(tmp_path, loops={5, 6})
    app = web.create_app(svc)
    app.config.update(TESTING=True)
    return app.test_client(), svc


def test_get_no_pedal_503(env):
    client, svc = env
    svc.mounted = False
    assert client.get("/api/loops/5").status_code == 503


def test_get_no_loop_404(env):
    client, _ = env
    assert client.get("/api/loops/9").status_code == 404


def test_get_out_of_range_400(env):
    client, _ = env
    assert client.get("/api/loops/200").status_code == 400


def test_get_streams_the_loop_as_a_download(env):
    client, _ = env
    rv = client.get("/api/loops/5")
    assert rv.status_code == 200
    assert rv.data == b"LOOPBYTES"
    assert "attachment" in rv.headers["Content-Disposition"]
    assert "loop-05.wav" in rv.headers["Content-Disposition"]


def test_get_when_the_loop_vanished_404(env, tmp_path):
    """Deleted on the pedal between the scan and the request."""
    client, svc = env
    svc.loop_path = lambda slot: tmp_path / "gone.wav"
    assert client.get("/api/loops/6").status_code == 404


def test_delete_no_loop_404(env):
    client, _ = env
    assert client.delete("/api/loops/9").status_code == 404


def test_delete_ok(env):
    client, svc = env
    rv = client.delete("/api/loops/5")
    assert rv.status_code == 200
    assert rv.get_json()["ok"] is True
    assert not svc.has_loop(5)


def test_delete_cross_site_rejected(env):
    client, _ = env
    rv = client.delete("/api/loops/5", headers={"Origin": "http://evil.example"})
    assert rv.status_code == 403
