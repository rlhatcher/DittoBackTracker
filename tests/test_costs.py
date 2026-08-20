"""Costs that matter on a Pi Zero 2 W.

These are not micro-benchmarks — they count operations. Each pins a specific
regression that is invisible locally and expensive on the device: a SQLite
round trip per file, a USB stat per slot, an ffprobe per track.
"""

import itertools
import types
import queue
import threading

import pytest

from ditto import config, core, db, media, pedal


@pytest.fixture
def svc(tmp_path, monkeypatch):
    """A Service read path with no threads, on its own database."""
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "state.db")
    for name in ("SOURCES", "STAGED", "LOOPS"):
        d = tmp_path / name.lower()
        monkeypatch.setattr(config, name, d)
        d.mkdir(parents=True, exist_ok=True)
    for attr in ("conn", "path"):
        if hasattr(db._local, attr):
            delattr(db._local, attr)

    s = core.Service.__new__(core.Service)
    s.fmt = dict(config.DEFAULT_FORMAT)
    s.fmt_source = "default"
    s.pedal_state = "absent"
    s.busy = s.busy_kind = s.progress = s.last_error = None
    s.ending = False
    s._loops = frozenset()
    # snapshot() reads these three off the updater now.
    s.updater = types.SimpleNamespace(revision="abc1234", available=False,
                                      remote_revision=None)
    s._snap_seq = itertools.count(1)
    # Enough of the machinery for _emit and the convert job to run.
    s._subs = []
    s._subs_lock = threading.Lock()
    s._work = queue.Queue()
    s._last_prog_emit = 0.0
    yield s
    for attr in ("conn", "path"):
        if hasattr(db._local, attr):
            delattr(db._local, attr)


def count_calls(monkeypatch, module, name):
    """Wrap a callable and return the list its calls are recorded in."""
    calls = []
    real = getattr(module, name)

    def counted(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(module, name, counted)
    return calls


def test_a_snapshot_reads_the_slot_table_once(svc, monkeypatch):
    """capacity()'s unmounted fallback used to re-read it, and unmounted is the
    normal state during a conversion — which is when this runs at 5 Hz."""
    db.library_add("a" * 20, "Track", 120.0)
    db.put_slot(1, "a" * 20, state="synced")
    calls = count_calls(monkeypatch, db, "all_slots")

    svc.snapshot()

    assert len(calls) == 1, f"the slot table was read {len(calls)} times"


def test_capacity_still_works_when_called_on_its_own(svc):
    """The slot list is an optional hand-off, not a required argument."""
    db.library_add("a" * 20, "Track", 90.0)
    db.put_slot(1, "a" * 20, state="synced")

    assert svc.capacity()["used_seconds"] == 90.0
    assert svc.capacity(db.all_slots())["used_seconds"] == 90.0


def test_the_collector_reads_the_library_once_not_once_per_file(svc,
                                                                monkeypatch):
    """The library is meant to grow to card capacity, so a per-file round trip
    against a synchronous=FULL database is O(library) on every boot."""
    for i in range(6):
        (config.SOURCES / f"{i:020d}.mp3").write_bytes(b"x")
    db.library_add("0" * 20, "Kept", 10.0)
    monkeypatch.setattr(core.Service, "_older_than", staticmethod(lambda f, c: False))
    per_file = count_calls(monkeypatch, db, "hash_in_library")
    bulk = count_calls(monkeypatch, db, "library_hashes")

    svc._gc()

    assert per_file == [], "the collector still asks once per file"
    assert len(bulk) == 1


def test_the_collector_still_keeps_what_the_library_knows(svc):
    """The cheaper query must not change which files survive."""
    kept = config.SOURCES / f"{'a' * 20}.mp3"
    orphan = config.SOURCES / f"{'b' * 20}.mp3"
    for f in (kept, orphan):
        f.write_bytes(b"x")
    db.library_add("a" * 20, "Kept", 10.0)
    import os
    import time
    old = time.time() - config.GC_GRACE_SECS - 60
    for f in (kept, orphan):
        os.utime(f, (old, old))

    svc._gc()

    assert kept.exists()
    assert not orphan.exists()


def test_convert_skips_the_probe_when_the_duration_is_known(tmp_path,
                                                            monkeypatch):
    """An ffprobe fork+exec+parse is a few hundred ms on a Pi Zero, and a
    multi-file drop pays it once per track, serialised behind the queue."""
    monkeypatch.setattr(config, "STAGED", tmp_path)
    probes = count_calls(monkeypatch, media, "probe")
    monkeypatch.setattr(media.subprocess, "Popen", _fake_popen)

    media.convert(tmp_path / "in.mp3", "a" * 20,
                  dict(config.DEFAULT_FORMAT), duration=210.0)

    assert probes == [], "convert probed despite being told the duration"


def test_convert_still_probes_when_the_duration_is_unknown(tmp_path,
                                                           monkeypatch):
    """Stubbed rather than counted-through: the real probe would shell out to
    ffprobe, which is the thing being avoided."""
    monkeypatch.setattr(config, "STAGED", tmp_path)
    probes = []
    monkeypatch.setattr(media, "probe", lambda src: (
        probes.append(src), media.AudioInfo("mp3", 44100, 2, 210.0))[1])
    monkeypatch.setattr(media.subprocess, "Popen", _fake_popen)

    media.convert(tmp_path / "in.mp3", "b" * 20, dict(config.DEFAULT_FORMAT))

    assert len(probes) == 1, "the fallback probe is gone"


class _FakeProc:
    returncode = 0

    def __init__(self, cmd, **kw):
        # ffmpeg's -progress stream; the last argument is the .part path.
        self.stdout = iter(["out_time_us=1000000\n"])
        self._out = cmd[-1]

    def __enter__(self):
        open(self._out, "wb").write(b"\0" * 64)   # something for the rename
        return self

    def __exit__(self, *a):
        return False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def _fake_popen(cmd, **kw):
    return _FakeProc(cmd, **kw)


def test_upload_auto_does_not_stat_the_pedal_per_slot(monkeypatch):
    """core.py's own comment says 99 stats over a ~1 MB/s USB link would be
    visibly slow; Service._loops is the cache that exists to avoid it."""
    from ditto import web

    seen = count_calls(monkeypatch, pedal, "has_loop")
    monkeypatch.setattr(pedal, "mounted", lambda: True)

    class FakeService:
        def has_loop(self, n):
            return False

        def upload(self, *a, **k):
            raise ValueError("not a readable audio file")

    app = web.create_app(FakeService())
    client = app.test_client()
    import io
    client.post("/api/upload", content_type="multipart/form-data",
                data={"file": (io.BytesIO(b"x"), "track.mp3")})

    assert seen == [], "upload_auto stats the pedal instead of using the cache"


def test_the_convert_job_hands_convert_the_duration_it_already_knows(svc,
                                                                    monkeypatch):
    """The saving only lands if the caller passes it. row["duration"] is joined
    from the library, where upload() stored what it probed."""
    h = "c" * 20
    db.library_add(h, "Known", 210.0)
    db.put_slot(1, h, state="converting")
    src = config.SOURCES / f"{h}.mp3"
    src.write_bytes(b"x")

    seen = {}
    monkeypatch.setattr(media, "convert",
                        lambda *a, **k: seen.update(k) or (config.STAGED / "x.wav"))

    svc._do_convert(1, h, src)

    assert seen.get("duration") == 210.0, (
        f"the convert job did not pass the known duration: {seen}")
