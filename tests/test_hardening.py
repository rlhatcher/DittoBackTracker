"""Power-loss hardening: durable ingest, startup sweep, newest-wins SSE.

There is no battery any more, so the plug can come out at any instant. These
cover the three places that assumption changed something.

Note the two fixtures. `paths` repoints the file-tree constants for tests that
only touch files; the tests that build a real Service deliberately use the
conftest temp tree instead, because db.conn() caches one connection per thread
against config.DB_PATH and repointing DATA underneath it would strand that
connection on a path the next test can't reach.
"""

import json
import os
import pathlib
import queue
import stat
import time

import pytest

from ditto import config, core


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """Repoint the file-tree constants at a throwaway tree.

    Each is derived from DATA at import time, so each has to be patched
    individually — patching DATA alone would leave the rest pointing at the
    conftest tree.
    """
    monkeypatch.setattr(config, "DATA", tmp_path)
    for name in ("SOURCES", "STAGED", "TRASH", "LOOPS"):
        d = tmp_path / name.lower()
        monkeypatch.setattr(config, name, d)
        d.mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def service():
    config.ensure_dirs()
    svc = core.Service()
    yield svc
    svc.shutdown(timeout=2.0)


# --- durable ingest ---------------------------------------------------------

def test_store_source_moves_the_file_intact(paths):
    tmp = paths / "tmpupload"
    tmp.write_bytes(b"audio bytes")
    stored = config.SOURCES / "abc123.mp3"

    core.Service._store_source(tmp, stored)

    assert stored.read_bytes() == b"audio bytes"
    assert not tmp.exists()


def test_store_source_survives_an_unfsyncable_directory(paths, monkeypatch):
    """A filesystem that refuses to fsync a *directory* must not fail the
    upload: the bytes are already durable by then, and only the name is at
    stake."""
    tmp = paths / "tmpupload"
    tmp.write_bytes(b"audio bytes")
    stored = config.SOURCES / "abc123.mp3"

    real_fsync = os.fsync
    refused = []

    def only_dirs_fail(fd):
        # Refuse directories, pass regular files through to the real call.
        # Blanket-failing os.fsync would raise on the file first and never
        # reach the directory branch this test is named for.
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            refused.append(fd)
            raise OSError("fsync not supported on directories")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", only_dirs_fail)
    core.Service._store_source(tmp, stored)

    assert stored.read_bytes() == b"audio bytes"
    # Without this the test would still pass if the directory fsync were
    # dropped altogether — the stub would simply never fire, and the tolerance
    # this test exists to prove would go untested.
    assert refused, "the directory fsync was never attempted"


def test_store_source_refuses_to_claim_an_unsyncable_file(paths, monkeypatch):
    """The file's own fsync is the whole guarantee. If it fails, the caller
    must not go on to record a row pointing at bytes that were never confirmed
    on the card."""
    tmp = paths / "tmpupload"
    tmp.write_bytes(b"audio bytes")
    stored = config.SOURCES / "abc123.mp3"

    def always_fail(fd):
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "fsync", always_fail)
    with pytest.raises(OSError):
        core.Service._store_source(tmp, stored)

    # sources/ is content-addressed, so an unconfirmed file left behind would be
    # adopted by the next upload of the same track instead of being rewritten.
    assert not stored.exists()


# --- the upload-temp sweep --------------------------------------------------

def test_sweep_takes_stale_upload_temps_and_leaves_fresh_ones(paths):
    stale = paths / "tmpstale"
    fresh = paths / "tmpfresh"
    other = paths / "state.db"
    for p in (stale, fresh, other):
        p.write_bytes(b"x")
    old = time.time() - config.UPLOAD_TEMP_KEEP_SECS - 60
    os.utime(stale, (old, old))

    core.Service._sweep_upload_temps()

    assert not stale.exists()
    assert fresh.exists(), "an upload in flight owns its temp file"
    assert other.exists(), "only tmp* is ours to delete"


def test_sweep_ignores_directories(paths):
    d = paths / "tmpdir"
    d.mkdir()
    old = time.time() - config.UPLOAD_TEMP_KEEP_SECS - 60
    os.utime(d, (old, old))

    core.Service._sweep_upload_temps()

    assert d.is_dir()


# --- the collector's grace window -------------------------------------------

def test_gc_spares_a_freshly_written_source(service):
    """upload() writes the file, then the slot row. A collector run landing in
    that window must not delete an upload that is moments from being
    referenced."""
    fresh = config.SOURCES / "0123456789abcdef0123.mp3"
    fresh.write_bytes(b"just uploaded, no row yet")

    service._gc()

    assert fresh.exists()
    fresh.unlink()


def test_gc_takes_an_old_unreferenced_source(service):
    orphan = config.SOURCES / "fedcba98765432100000.mp3"
    orphan.write_bytes(b"nothing references this")
    old = time.time() - config.GC_GRACE_SECS - 60
    os.utime(orphan, (old, old))

    service._gc()

    assert not orphan.exists()


def test_gc_takes_an_interrupted_transcode_at_any_age(service):
    """A .part file is never referenced by anything, however new it is."""
    part = config.STAGED / "0123456789abcdef0123-pcm_s24le-44100-1.wav.part"
    part.write_bytes(b"half a wav")

    service._gc()

    assert not part.exists()


# --- the startup pass -------------------------------------------------------

def test_startup_gc_clears_an_interrupted_transcode():
    """A .part file is a transcode a pulled plug interrupted. _halt never ran,
    so only the startup pass can reach it."""
    config.ensure_dirs()
    part = config.STAGED / "deadbeef-pcm_s24le-44100-1.wav.part"
    part.write_bytes(b"half a wav")

    svc = core.Service()
    try:
        deadline = time.time() + 5.0
        while part.exists() and time.time() < deadline:
            time.sleep(0.05)
        assert not part.exists()
    finally:
        svc.shutdown(timeout=2.0)


# --- newest-wins SSE --------------------------------------------------------

def test_emit_drops_the_backlog_not_the_new_frame(service):
    """Each frame is whole state, so a slow client must get the newest one
    rather than being stranded on stale state behind a full queue."""
    q = service.subscribe()
    while True:
        try:
            q.put_nowait({"stale": True})
        except queue.Full:
            break

    service.last_error = "the newest thing that happened"
    service._emit()

    got = q.get_nowait()
    assert got.get("error") == "the newest thing that happened"


def test_emit_reaches_every_subscriber(service):
    qs = [service.subscribe() for _ in range(3)]
    service.last_error = "broadcast"
    service._emit()
    for q in qs:
        frames = []
        while not q.empty():
            frames.append(q.get_nowait())
        assert any(f.get("error") == "broadcast" for f in frames)


# --- the SSE initial-frame handoff ------------------------------------------

def test_the_stream_never_sends_a_frame_older_than_the_one_before(service,
                                                                  monkeypatch):
    """events() subscribes before it takes its initial snapshot, so the queue
    can already hold an older frame. Sending it after the initial one would roll
    the UI backwards.

    Deterministic: the stale frame is placed on the queue before the stream is
    opened, and a newer one after, so the ordering under test is fixed rather
    than raced.
    """
    from ditto import web

    stale = service.snapshot()                 # built first, so lowest seq
    stale["error"] = "STALE — must never be sent"

    q = queue.Queue(maxsize=64)
    q.put(stale)
    monkeypatch.setattr(service, "subscribe", lambda: q)
    monkeypatch.setattr(service, "unsubscribe", lambda _q: None)

    client = web.create_app(service).test_client()
    resp = client.get("/api/events")
    stream = resp.response

    first = json.loads(next(stream).decode().removeprefix("data: ").strip())

    newer = service.snapshot()                 # built last, so highest seq
    newer["error"] = "NEWER"
    q.put(newer)

    second = json.loads(next(stream).decode().removeprefix("data: ").strip())
    resp.close()

    assert first["seq"] > stale["seq"], "precondition: the queued frame is older"
    assert second["error"] == "NEWER", f"stale frame was sent: {second['error']!r}"
    assert second["seq"] > first["seq"]


def test_store_source_syncs_before_it_publishes_the_name(paths, monkeypatch):
    """Order matters, not just that both happen. sources/ is content-addressed,
    so a name published ahead of its confirmed bytes leaves a window where a
    power cut strands an unconfirmed file that the next upload of the same
    track would adopt — skipping the write on stored.exists()."""
    tmp = paths / "tmpupload"
    tmp.write_bytes(b"audio bytes")
    stored = config.SOURCES / "abc123.mp3"

    events = []
    real_fsync, real_replace = os.fsync, pathlib.Path.replace

    def track_fsync(fd):
        events.append("fsync-dir" if stat.S_ISDIR(os.fstat(fd).st_mode)
                      else "fsync-file")
        return real_fsync(fd)

    def track_replace(self, target):
        events.append("rename")
        return real_replace(self, target)

    monkeypatch.setattr(os, "fsync", track_fsync)
    monkeypatch.setattr(pathlib.Path, "replace", track_replace)
    core.Service._store_source(tmp, stored)

    assert "fsync-file" in events and "rename" in events
    assert events.index("fsync-file") < events.index("rename"), \
        f"the name was published before the bytes were confirmed: {events}"
