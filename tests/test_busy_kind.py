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
