"""Loop-file helpers in pedal.py, exercised against a temp dir standing in for
the mounted pedal. No real device or ffmpeg needed."""

import pytest

from ditto import config, pedal


@pytest.fixture
def mount(tmp_path, monkeypatch):
    m = tmp_path / "mount"
    loops = tmp_path / "loops"
    m.mkdir()
    loops.mkdir()
    monkeypatch.setattr(config, "MOUNT", m)
    monkeypatch.setattr(config, "LOOPS", loops)
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


def test_copy_loop_reproduces_bytes(mount):
    data = b"RIFF\x00\x00loopdata" * 4096
    (_slot_dir(mount, 5) / "LOOP.WAV").write_bytes(data)
    dest = config.LOOPS / "slot-05.wav"

    pedal.copy_loop(5, dest)

    assert dest.read_bytes() == data
    assert not list(config.LOOPS.glob("~loop*.tmp"))   # temp cleaned up


def test_copy_loop_missing_source_raises_and_leaves_no_temp(mount):
    _slot_dir(mount, 6)                                 # slot exists, no loop
    dest = config.LOOPS / "slot-06.wav"

    with pytest.raises(FileNotFoundError):
        pedal.copy_loop(6, dest)

    assert not dest.exists()
    assert not list(config.LOOPS.glob("~loop*.tmp"))


def test_remove_loop_removes_only_the_loop(mount):
    d = _slot_dir(mount, 5)
    (d / "LOOP.WAV").write_bytes(b"loop")
    (d / "BT.WAV").write_bytes(b"backing")

    pedal.remove_loop(5)

    assert not (d / "LOOP.WAV").exists()
    assert (d / "BT.WAV").exists()                      # backing track survives
    assert d.is_dir()                                   # slot dir survives


def test_remove_loop_absent_is_noop(mount):
    _slot_dir(mount, 7)
    pedal.remove_loop(7)                                # must not raise
    assert not pedal.has_loop(7)


def test_run_turns_a_missing_mount_binary_into_a_pedal_error(monkeypatch):
    """A missing or non-executable mount(8) raises OSError, which is not a
    PedalError — so it escapes the monitor's handler and logs a traceback every
    poll, forever, on a device with a small journal."""
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
