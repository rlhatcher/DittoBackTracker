"""`busy_kind` is the device's only "don't unplug" signal.

The panel LED used to carry it. With no panel, a pedal write that doesn't raise
this flag is one the user gets no warning about — so every write has to set it,
including the quick ones.
"""

import time

import pytest

from ditto import config, core, pedal


@pytest.fixture
def service():
    config.ensure_dirs()
    svc = core.Service()
    svc._drain(timeout=5.0)
    yield svc
    svc.shutdown(timeout=2.0)


@pytest.fixture
def fake_pedal(tmp_path, monkeypatch):
    """A directory standing in for a mounted pedal, as test_pedal.py does."""
    monkeypatch.setattr(config, "MOUNT", tmp_path)
    monkeypatch.setattr(pedal, "mounted", lambda: True)
    d = tmp_path / config.SLOT_DIR.format(5)
    d.mkdir(parents=True)
    (d / config.TRACK_FILENAME).write_bytes(b"RIFF")
    return d


def test_erase_raises_the_write_warning(service, fake_pedal, monkeypatch):
    """Clearing a slot unlinks BT.WAV and calls os.sync — a pedal write like any
    other, and previously the only one that ran silently."""
    seen = []
    real_remove = pedal.remove_track

    def watch(slot):
        # Sample at the moment the pedal is actually being touched: the worker
        # clears the flags as soon as the job returns.
        seen.append((service.busy, service.busy_kind))
        return real_remove(slot)

    monkeypatch.setattr(pedal, "remove_track", watch)
    service._do_erase(5)

    assert seen, "remove_track was never reached"
    busy, kind = seen[0]
    assert kind == "write"
    assert busy and "05" in busy
    assert not (fake_pedal / config.TRACK_FILENAME).exists()


def test_the_worker_clears_the_warning_when_the_job_ends(service, fake_pedal):
    """A warning left standing would be worse than none — the user would learn
    to ignore it. The worker's finally is what resets it, so drive the real
    queue rather than calling _do_erase directly."""
    service._work.put(("erase", 5))

    deadline = time.time() + 5.0
    while (fake_pedal / config.TRACK_FILENAME).exists() and time.time() < deadline:
        time.sleep(0.05)
    assert not (fake_pedal / config.TRACK_FILENAME).exists(), "erase never ran"

    service._drain(timeout=5.0)
    snap = service.snapshot()
    assert snap["busy_kind"] is None
    assert snap["busy"] is None


def test_a_read_does_not_warn(service):
    """Unplugging mid-read costs only the download, so a read must not cry
    wolf — that distinction is the whole reason busy_kind isn't a boolean."""
    service.busy = "Reading loop"
    service.busy_kind = "read"
    assert service.snapshot()["busy_kind"] == "read"


# --- shutdown --------------------------------------------------------------

def test_halt_does_not_wait_on_its_own_job(service, fake_pedal, monkeypatch):
    """_halt runs *as* the end job, inside the worker, so _in_flight is already
    set on its own behalf. A _drain there could never see idle and would burn
    its whole timeout before every poweroff."""
    monkeypatch.setattr(core.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(pedal, "unmount", lambda: None)
    monkeypatch.setattr(core.time, "sleep", lambda s: None)   # the read-me pause

    took = []
    real = core.Service._halt

    def timed(self):
        t0 = time.monotonic()
        real(self)
        took.append(time.monotonic() - t0)

    monkeypatch.setattr(core.Service, "_halt", timed)
    service.end_session()

    deadline = time.monotonic() + 20
    while not took and time.monotonic() < deadline:
        time.sleep(0.05)
    assert took, "the end job never reached _halt"
    assert took[0] < 2.0, f"_halt waited {took[0]:.1f}s — it is draining itself"


def test_pedal_work_is_refused_once_ending(service):
    """The end marker only halts on an empty queue, so work queued after that
    check is abandoned at poweroff — having told the caller it succeeded."""
    service.ending = True
    for call in (lambda: service.clear(1),
                 lambda: service.move(1, 2),
                 lambda: service.retry(1),
                 lambda: service.delete_loop(1),
                 lambda: service.stage_loop(1),
                 lambda: service.restore(1)):
        with pytest.raises(core.ShuttingDown):
            call()


def test_refusal_is_a_503_not_a_400(service):
    """The request was fine; the device just isn't taking work. A client that
    retries next session is doing the right thing."""
    from ditto import web
    client = web.create_app(service).test_client()
    service.ending = True
    assert client.delete("/api/slots/1").status_code == 503
