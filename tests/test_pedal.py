"""pedal.py against a temp dir standing in for the mounted pedal."""

import pathlib
import queue
import threading

import pytest

from ditto import config, pedal


@pytest.fixture
def mount(tmp_path, monkeypatch):
    m = tmp_path / "mount"
    m.mkdir()
    monkeypatch.setattr(config, "MOUNT", m)
    return m


def _slot_dir(mount, n):
    d = mount / config.SLOT_DIR.format(n)
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_loop_path(mount):
    assert pedal.loop_path(5) == mount / "05track" / "LOOP.WAV"


def test_has_loop(mount):
    (_slot_dir(mount, 5) / "LOOP.WAV").write_bytes(b"abc")
    assert pedal.has_loop(5)
    assert not pedal.has_loop(6)


def test_remove_loop_removes_only_the_loop(mount):
    d = _slot_dir(mount, 5)
    (d / "LOOP.WAV").write_bytes(b"loop")
    (d / "BT.WAV").write_bytes(b"backing")

    pedal.remove_loop(5)

    assert not (d / "LOOP.WAV").exists()
    assert (d / "BT.WAV").exists()
    assert d.is_dir()


def test_remove_loop_absent_is_noop(mount):
    d = _slot_dir(mount, 7)
    (d / config.TRACK_FILENAME).write_bytes(b"a real track")

    pedal.remove_loop(7)

    assert (d / config.TRACK_FILENAME).read_bytes() == b"a real track"


def test_run_turns_a_missing_mount_binary_into_a_pedal_error(monkeypatch):
    """A missing mount(8) raises OSError, which would otherwise escape the
    monitor's handler and log a traceback every poll."""
    def no_such_binary(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "mount")

    monkeypatch.setattr(pedal.subprocess, "run", no_such_binary)
    with pytest.raises(pedal.PedalError, match="could not run"):
        pedal._run(["mount", "/media/ditto"], "mount")


def test_run_still_maps_a_timeout(monkeypatch):
    def too_slow(*a, **k):
        raise pedal.subprocess.TimeoutExpired(cmd="mount", timeout=30)

    monkeypatch.setattr(pedal.subprocess, "run", too_slow)
    with pytest.raises(pedal.PedalError, match="timed out"):
        pedal._run(["mount", "/media/ditto"], "mount")


def test_write_track_lands_the_bytes_and_leaves_no_temp(mount, tmp_path):
    wav = tmp_path / "staged.wav"
    wav.write_bytes(b"RIFF" + b"\x00" * 4096)

    pedal.write_track(5, wav)

    dest = pedal.track_path(5)
    assert dest.read_bytes() == wav.read_bytes()
    assert list(dest.parent.glob("~bt*.tmp")) == [], "a temp file was left behind"
    assert dest.stat().st_mode & 0o777 == 0o644


def test_write_track_creates_the_slot_directory(mount, tmp_path):
    wav = tmp_path / "staged.wav"
    wav.write_bytes(b"RIFF")

    pedal.write_track(42, wav)

    assert pedal.track_path(42).is_file()


def test_write_track_cleans_up_when_the_source_vanishes(mount, tmp_path):
    """The half-written temp must not be left on the pedal's capacity."""
    missing = tmp_path / "gone.wav"

    with pytest.raises(OSError):
        pedal.write_track(6, missing)

    slot = mount / config.SLOT_DIR.format(6)
    assert list(slot.glob("~bt*.tmp")) == [], "a temp file survived the failure"
    assert not pedal.track_path(6).exists()


def test_write_track_replaces_an_existing_track_atomically(mount, tmp_path):
    old = tmp_path / "old.wav"
    old.write_bytes(b"OLD" * 100)
    new = tmp_path / "new.wav"
    new.write_bytes(b"NEW" * 200)
    pedal.write_track(7, old)

    pedal.write_track(7, new)

    assert pedal.track_path(7).read_bytes() == new.read_bytes()
    assert list(pedal.track_path(7).parent.glob("~bt*.tmp")) == []


def test_clean_temp_files_removes_interrupted_writes(mount, monkeypatch):
    monkeypatch.setattr(pedal, "mounted", lambda: True)
    d = _slot_dir(mount, 3)
    (d / "~btabc.tmp").write_bytes(b"half a wav")
    (d / config.TRACK_FILENAME).write_bytes(b"a real track")
    (d / config.LOOP_FILENAME).write_bytes(b"a real loop")

    removed = pedal.clean_temp_files()

    assert removed == 1
    assert list(d.glob("~bt*.tmp")) == []
    assert (d / config.TRACK_FILENAME).exists(), "a real track was deleted"
    assert (d / config.LOOP_FILENAME).exists(), "a recorded loop was deleted"


def test_a_write_is_flushed_before_the_database_records_it(tmp_path,
                                                          monkeypatch):
    """_do_write runs os.sync() before mark_synced, so the database never says
    a track is on the pedal while a power cut could still lose it."""
    from ditto import core

    order = []
    monkeypatch.setattr(core.pedal, "mounted", lambda: True)
    monkeypatch.setattr(core.pedal, "write_track",
                        lambda slot, wav: order.append("write"))
    monkeypatch.setattr(core.pedal, "capacity", lambda: (1 << 30, 1 << 30))
    monkeypatch.setattr(core.pedal, "track_path",
                        lambda slot: pathlib.Path("/nonexistent/BT.WAV"))
    monkeypatch.setattr(core.os, "sync", lambda: order.append("sync"))
    monkeypatch.setattr(core.db, "mark_synced",
                        lambda slot, h: order.append("mark_synced"))

    svc = core.Service.__new__(core.Service)
    svc.busy = svc.progress = None
    svc._subs, svc._subs_lock = [], threading.Lock()
    svc._work = queue.Queue()
    monkeypatch.setattr(core.Service, "_emit", lambda self: None)

    staged = tmp_path / "x.wav"
    staged.write_bytes(b"\0" * 128)
    monkeypatch.setattr(core.db, "get_slot",
                        lambda s: {"slot": s, "source_hash": "a" * 20,
                                   "state": "staged", "display_name": "T",
                                   "duration": 1.0, "synced_hash": None})
    monkeypatch.setattr(core.media, "staged_path", lambda h: staged)

    svc._do_write(1)

    assert order == ["write", "sync", "mark_synced"], (
        f"the write was recorded before it was flushed: {order}")
