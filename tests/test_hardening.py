"""Power-loss hardening: durable ingest, startup sweep, newest-wins SSE.

There is no battery any more, so the plug can come out at any instant. These
cover the three places that assumption changed something.

Fixtures come from conftest: `data_tree` for the tests that only touch files,
`service` for the ones that need the worker. Both give each test its own
database and file tree, so no test has to pick a hash nobody else is using.
"""

import json
import os
import pathlib
import queue
import stat
import time

import pytest

from ditto import config, core, db


# --- durable ingest ---------------------------------------------------------

def test_store_source_moves_the_file_intact(data_tree):
    tmp = data_tree / "tmpupload"
    tmp.write_bytes(b"audio bytes")
    stored = config.SOURCES / "abc123.mp3"

    core.Service._store_source(tmp, stored)

    assert stored.read_bytes() == b"audio bytes"
    assert not tmp.exists()


def test_store_source_survives_an_unfsyncable_directory(data_tree, monkeypatch):
    """A filesystem that refuses to fsync a *directory* must not fail the
    upload: the bytes are already durable by then, and only the name is at
    stake."""
    tmp = data_tree / "tmpupload"
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


def test_store_source_refuses_to_claim_an_unsyncable_file(data_tree, monkeypatch):
    """The file's own fsync is the whole guarantee. If it fails, the caller
    must not go on to record a row pointing at bytes that were never confirmed
    on the card."""
    tmp = data_tree / "tmpupload"
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

def test_sweep_takes_stale_upload_temps_and_leaves_fresh_ones(data_tree):
    stale = data_tree / "tmpstale"
    fresh = data_tree / "tmpfresh"
    other = data_tree / "state.db"
    for p in (stale, fresh, other):
        p.write_bytes(b"x")
    old = time.time() - config.UPLOAD_TEMP_KEEP_SECS - 60
    os.utime(stale, (old, old))

    core.Service._sweep_upload_temps()

    assert not stale.exists()
    assert fresh.exists(), "an upload in flight owns its temp file"
    assert other.exists(), "only tmp* is ours to delete"


def test_sweep_ignores_directories(data_tree):
    d = data_tree / "tmpdir"
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
    fresh = config.SOURCES / f"{'b' * 20}.mp3"
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
    part = config.STAGED / f"{'c' * 20}-pcm_s24le-44100-1.wav.part"
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


def test_store_source_syncs_before_it_publishes_the_name(data_tree, monkeypatch):
    """Order matters, not just that both happen. sources/ is content-addressed,
    so a name published ahead of its confirmed bytes leaves a window where a
    power cut strands an unconfirmed file that the next upload of the same
    track would adopt — skipping the write on stored.exists()."""
    tmp = data_tree / "tmpupload"
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


def test_gc_keeps_a_staged_file_cached_under_a_different_format(service):
    """The startup collector runs before any pedal has been probed, so self.fmt
    is still the default. Matching whole staged filenames — which carry a format
    tag — would delete every WAV cached under the pedal's real format on every
    boot, costing a full re-transcode of every assigned slot.
    """
    h = "0123456789abcdef0123"
    db.library_add(h, "Assigned", 120.0)
    db.put_slot(1, h, state="synced")
    # Cached under a format the pedal probed last session, not today's default.
    other = config.STAGED / f"{h}-pcm_s16le-48000-2.wav"
    other.write_bytes(b"a cached transcode")
    old = time.time() - config.GC_GRACE_SECS - 60
    os.utime(other, (old, old))
    assert service.fmt == dict(config.DEFAULT_FORMAT), "precondition: unprobed"
    # A file the same pass must delete. _gc catches and logs its own exceptions,
    # so without a positive control it could bail before the staged-file loop
    # and this test would pass on a collector that never ran.
    canary = config.STAGED / "99999999999999999999-pcm_s24le-44100-1.wav"
    canary.write_bytes(b"no slot wants this")
    old = time.time() - config.GC_GRACE_SECS - 60
    os.utime(canary, (old, old))

    service._gc()

    assert not canary.exists(), "the staged-file loop did not run"
    assert other.exists(), "a staged file for an assigned slot was collected"


def test_gc_still_drops_staged_files_for_unassigned_tracks(service):
    """The reason the predicate is per-slot and not per-library-track: mono
    24-bit at 44.1 kHz is 132 kB/s, so caching one per library track would be
    gigabytes behind a few hundred MB of music."""
    h = "a" * 20
    db.library_add(h, "In the library, not on the pedal", 120.0)
    stale = config.STAGED / f"{h}-pcm_s24le-44100-1.wav"
    stale.write_bytes(b"cache for a track no slot wants")
    old = time.time() - config.GC_GRACE_SECS - 60
    os.utime(stale, (old, old))

    service._gc()

    assert not stale.exists()


# --- shutdown ---------------------------------------------------------------

def test_shutdown_finishes_queued_work_before_it_stops(service, monkeypatch):
    """The wake-up sentinel must not become a way to abandon real work.
    shutdown drains first, and only then unblocks the worker."""
    ran = []
    monkeypatch.setattr(core.Service, "_do_erase",
                        lambda self, slot: ran.append(slot))
    monkeypatch.setattr(core.pedal, "unmount", lambda: None)
    service._work.put(("erase", 5))
    service._work.put(("erase", 6))

    service.shutdown(timeout=5.0)

    assert ran == [5, 6], f"queued work was abandoned: {ran}"


def test_shutdown_stops_the_worker_threads(service, monkeypatch):
    """shutdown promises the threads are done when it returns — the joins are
    bounded, so a worker that ignored the stop would outlive the call and go on
    touching a pedal the caller believes is unmounted."""
    monkeypatch.setattr(core.pedal, "unmount", lambda: None)

    service.shutdown(timeout=5.0)

    alive = [t.name for t in service._threads if t.is_alive()]
    assert not alive, f"shutdown returned with threads still running: {alive}"


def test_shutdown_is_idempotent(service, monkeypatch):
    monkeypatch.setattr(core.pedal, "unmount", lambda: None)
    service.shutdown(timeout=5.0)
    service.shutdown(timeout=5.0)      # must not raise or hang
