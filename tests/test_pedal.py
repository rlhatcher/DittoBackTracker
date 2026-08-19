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


# --- the atomic copy both directions share ----------------------------------

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
    """A staged file can disappear between the caller's check and the copy.
    The half-written temp must not be left on the pedal's limited capacity."""
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


def test_copy_loop_lands_the_bytes_locally(mount, tmp_path):
    d = _slot_dir(mount, 8)
    (d / config.LOOP_FILENAME).write_bytes(b"LOOP" + b"\x00" * 2048)
    dest = tmp_path / "staged-loop.wav"

    pedal.copy_loop(8, dest)

    assert dest.read_bytes() == (d / config.LOOP_FILENAME).read_bytes()
    assert list(dest.parent.glob("~loop*.tmp")) == []


def test_copy_loop_raises_when_the_loop_has_gone(mount, tmp_path):
    """It can be deleted on the pedal between has_loop() and here."""
    _slot_dir(mount, 9)
    dest = tmp_path / "staged-loop.wav"

    with pytest.raises(FileNotFoundError):
        pedal.copy_loop(9, dest)

    assert not dest.exists()
    assert list(dest.parent.glob("~loop*.tmp")) == [], "a temp file survived"


def test_clean_temp_files_removes_interrupted_writes(mount, monkeypatch):
    """~bt*.tmp is invisible to the pedal but eats its very limited capacity."""
    # The temp dir standing in for the mount is not a real mount point, and
    # clean_temp_files refuses to touch anything unless it believes one is up.
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


def _fail_dir_fsync(monkeypatch, err):
    """Make fsync fail for directories only, leaving file fsync working."""
    import os as _os
    import stat as _stat
    real = _os.fsync

    def only_dirs_fail(fd):
        if _stat.S_ISDIR(_os.fstat(fd).st_mode):
            raise OSError(err, "boom")
        return real(fd)

    monkeypatch.setattr(pedal.os, "fsync", only_dirs_fail)


def test_a_fat_driver_without_directory_fsync_is_silent(mount, tmp_path,
                                                        monkeypatch, caplog):
    """Plenty of FAT drivers simply do not implement it. That is expected, and
    the caller's os.sync() covers it, so it must not cry wolf."""
    import errno as _errno
    wav = tmp_path / "staged.wav"
    wav.write_bytes(b"RIFF")
    _fail_dir_fsync(monkeypatch, _errno.EINVAL)

    with caplog.at_level("WARNING"):
        pedal.write_track(11, wav)

    assert pedal.track_path(11).read_bytes() == b"RIFF"
    assert caplog.records == [], "an expected condition was reported as a problem"


def test_a_failing_card_is_reported_but_does_not_fail_the_write(
        mount, tmp_path, monkeypatch, caplog):
    """A real I/O error must not look like the benign case. It still must not
    raise: the rename already happened, so the file is in place — failing here
    would report a loss that did not occur."""
    import errno as _errno
    wav = tmp_path / "staged.wav"
    wav.write_bytes(b"RIFF")
    _fail_dir_fsync(monkeypatch, _errno.EIO)

    with caplog.at_level("WARNING"):
        pedal.write_track(12, wav)

    assert pedal.track_path(12).read_bytes() == b"RIFF", "the write was lost"
    assert any("may not survive a power cut" in r.getMessage()
               for r in caplog.records), "a failing card went unreported"
