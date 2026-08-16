"""Power-loss hardening: durable ingest, startup sweep, newest-wins SSE.

There is no battery any more, so the plug can come out at any instant. These
cover the three places that assumption changed something.

Note the two fixtures. `paths` repoints the file-tree constants for tests that
only touch files; the tests that build a real Service deliberately use the
conftest temp tree instead, because db.conn() caches one connection per thread
against config.DB_PATH and repointing DATA underneath it would strand that
connection on a path the next test can't reach.
"""

import os
import queue
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
    """A filesystem that refuses to fsync a directory must not fail the upload
    — the rename has already happened by then."""
    tmp = paths / "tmpupload"
    tmp.write_bytes(b"audio bytes")
    stored = config.SOURCES / "abc123.mp3"

    def boom(fd):
        raise OSError("fsync not supported")

    monkeypatch.setattr(os, "fsync", boom)
    core.Service._store_source(tmp, stored)

    assert stored.read_bytes() == b"audio bytes"


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
