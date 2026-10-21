"""Durable ingest, the boot sweep, newest-wins SSE, and shutdown.

Fixtures come from conftest: `data_tree` for the tests that only touch files,
`service` for the ones that need the worker.
"""

import json
import os
import pathlib
import queue

import conftest
import pytest

from ditto import config, core, db, media, pedal, update

# --- durable ingest ---------------------------------------------------------

def test_store_source_moves_the_file_intact(data_tree):
    tmp = data_tree / "tmpupload"
    tmp.write_bytes(b"audio bytes")
    stored = config.SOURCES / "abc123.mp3"

    core.Service._store_source(tmp, stored)

    assert stored.read_bytes() == b"audio bytes"
    assert not tmp.exists()


def test_store_source_refuses_to_claim_an_unsyncable_file(data_tree, monkeypatch):
    """If the fsync fails the caller must not record a row pointing at bytes
    that were never confirmed on the card."""
    tmp = data_tree / "tmpupload"
    tmp.write_bytes(b"audio bytes")
    stored = config.SOURCES / "abc123.mp3"

    def always_fail(fd):
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "fsync", always_fail)
    with pytest.raises(OSError):
        core.Service._store_source(tmp, stored)

    # sources/ is content-addressed: an unconfirmed file left behind would be
    # adopted by the next upload of the same track.
    assert not stored.exists()


def test_store_source_syncs_before_it_publishes_the_name(data_tree, monkeypatch):
    tmp = data_tree / "tmpupload"
    tmp.write_bytes(b"audio bytes")
    stored = config.SOURCES / "abc123.mp3"

    events = []
    real_fsync, real_replace = os.fsync, pathlib.Path.replace
    monkeypatch.setattr(os, "fsync",
                        lambda fd: (events.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(pathlib.Path, "replace",
                        lambda self, t: (events.append("rename"),
                                         real_replace(self, t))[1])

    core.Service._store_source(tmp, stored)

    assert events == ["fsync", "rename"], \
        f"the name was published before the bytes were confirmed: {events}"


# --- the boot sweep ---------------------------------------------------------

def _boot():
    """A Service constructed and stopped, for what its constructor sweeps."""
    svc = core.Service()
    svc.shutdown(timeout=2.0)


def test_the_source_is_on_disk_before_its_library_row_exists(service,
                                                             monkeypatch,
                                                             tmp_path):
    """Commit order. A file with no row is swept at boot; a row with no file
    is an error slot the user has to clear, so the file lands first."""
    src = tmp_path / "upload.mp3"
    src.write_bytes(b"pretend audio")
    h = "cccccccccccccccccccc"
    monkeypatch.setattr(core.media, "probe",
                        lambda p: core.media.AudioInfo("mp3", 44100, 2, 90.0))
    monkeypatch.setattr(core.media, "file_hash", lambda p: h)
    on_disk = []
    real_add = core.db.library_add

    def spy(*args):
        on_disk.append(service.source_for(h) is not None)
        return real_add(*args)

    monkeypatch.setattr(core.db, "library_add", spy)
    service.add_to_library(src, "Track")

    assert on_disk == [True], "the row was committed before its bytes landed"


def test_the_sweep_takes_what_nothing_references(data_tree):
    """At boot nothing is in flight, so anything unreferenced is garbage: an
    interrupted transcode, a stranded upload temporary, a staged WAV for a
    track no slot holds, a source for a track the library has forgotten."""
    part = config.STAGED / "deadbeef.wav.part"
    temp = data_tree / "tmpabc123"
    staged = config.STAGED / f"{'a' * 20}.wav"
    source = config.SOURCES / f"{'b' * 20}.mp3"
    for p in (part, temp, staged, source):
        p.write_bytes(b"x")

    _boot()

    assert not any(p.exists() for p in (part, temp, staged, source))


def test_the_sweep_leaves_what_the_database_knows(data_tree):
    db.library_add("c" * 20, "Kept", 10.0)
    db.library_add("d" * 20, "On the pedal", 10.0)
    db.put_slot(1, "d" * 20, state="synced")
    source = config.SOURCES / f"{'c' * 20}.mp3"
    staged = config.STAGED / f"{'d' * 20}.wav"
    other = data_tree / "state.db-journal"
    for p in (source, staged, other):
        p.write_bytes(b"x")

    _boot()

    assert source.exists(), "a library track's audio was deleted"
    assert staged.exists(), "a staged WAV for an assigned slot was deleted"
    assert other.exists(), "only tmp* in the data directory is ours"


# --- files go when their last reference does --------------------------------

def test_forget_deletes_the_audio_and_the_staged_wav(service):
    h = "e" * 20
    db.library_add(h, "Doomed", 10.0)
    source = config.SOURCES / f"{h}.mp3"
    staged = config.STAGED / f"{h}.wav"
    source.write_bytes(b"x")
    staged.write_bytes(b"x")

    assert service.forget(h) == ("deleted", [])

    assert not source.exists() and not staged.exists()


def test_clearing_the_last_slot_drops_the_staged_wav(service, monkeypatch):
    """The staged WAV is a cache bounded by the slots. A track in two slots
    keeps it until the second is cleared."""
    monkeypatch.setattr(service, "source_for", lambda h: None)
    h = "f" * 20
    db.library_add(h, "Twice", 10.0)
    db.put_slot(1, h, state="synced")
    db.put_slot(2, h, state="synced")
    staged = config.STAGED / f"{h}.wav"
    staged.write_bytes(b"x")

    service.clear(1)
    assert staged.exists(), "dropped while another slot still holds the track"
    service.clear(2)
    assert not staged.exists()


# --- newest-wins SSE --------------------------------------------------------

def test_emit_drops_the_backlog_not_the_new_frame(service):
    """Each frame is whole state, so a slow client must get the newest one."""
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


def test_the_stream_never_sends_a_frame_older_than_the_one_before(service,
                                                                  monkeypatch):
    """events() subscribes before its initial snapshot, so the queue can hold
    an older frame; sending it afterwards would roll the UI backwards."""
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


# --- shutdown ---------------------------------------------------------------

def test_shutdown_finishes_queued_work_before_it_stops(service, monkeypatch):
    ran = []
    monkeypatch.setattr(core.Service, "_do_erase",
                        lambda self, slot: ran.append(slot))
    monkeypatch.setattr(core.pedal, "unmount", lambda: None)
    service._work.put(("erase", 5))
    service._work.put(("erase", 6))

    service.shutdown(timeout=5.0)

    assert ran == [5, 6], f"queued work was abandoned: {ran}"


def test_shutdown_stops_the_worker_threads(service, monkeypatch):
    monkeypatch.setattr(core.pedal, "unmount", lambda: None)

    service.shutdown(timeout=5.0)

    alive = [t.name for t in service._threads if t.is_alive()]
    assert not alive, f"shutdown returned with threads still running: {alive}"


def test_shutdown_is_idempotent(service, monkeypatch):
    monkeypatch.setattr(core.pedal, "unmount", lambda: None)
    service.shutdown(timeout=5.0)
    service.shutdown(timeout=5.0)      # must not raise or hang


def test_the_suite_cannot_kill_itself():
    """A successful update ends by sending this process SIGTERM. conftest
    replaces that; this is what says so out loud, so the seam cannot be
    removed silently."""
    before = len(conftest.restart_requests)

    update.Updater._request_restart()

    assert conftest.restart_requests[before:] == [True], \
        "the guard should stand in for the real signal"


def test_the_pedal_is_not_reported_mounted_until_its_loops_are_known(service,
                                                                     monkeypatch):
    """upload_auto and _plan take "mounted" as their licence to trust _loops,
    so the flag must be published after the scan, not before."""
    seen = {}

    def has_loop(n):
        seen.setdefault("mounted", service.mounted)
        return n == 7

    monkeypatch.setattr(pedal, "present", lambda: True)
    monkeypatch.setattr(pedal, "mount", lambda: None)
    monkeypatch.setattr(pedal, "clean_temp_files", lambda: None)
    monkeypatch.setattr(pedal, "occupied_slots", lambda: set())
    monkeypatch.setattr(pedal, "has_loop", has_loop)

    service.pedal_state = "absent"
    service._loops = frozenset()
    service._tick_pedal()

    assert seen, "the loop scan never ran"
    assert seen["mounted"] is False, \
        "mounted was already true while the loop cache was still being built"
    assert service.mounted and 7 in service._loops


def test_the_pedal_is_not_reported_mounted_until_its_tracks_are_known(
        service, monkeypatch):
    """kept_slots() reads _tracks once the pedal reads as mounted, so an
    upload in between would miss an unknown BT.WAV and write over it."""
    seen = {}

    def occupied_slots():
        seen.setdefault("mounted", service.mounted)
        return {7: 1}

    monkeypatch.setattr(pedal, "present", lambda: True)
    monkeypatch.setattr(pedal, "mount", lambda: None)
    monkeypatch.setattr(pedal, "clean_temp_files", lambda: None)
    monkeypatch.setattr(pedal, "occupied_slots", occupied_slots)
    monkeypatch.setattr(pedal, "has_loop", lambda n: False)

    service.pedal_state = "absent"
    service._tick_pedal()

    assert seen["mounted"] is False, \
        "mounted was already true while the track scan was still running"
    assert service.mounted and service.kept_slots() == {7}


# --- what the page lists is what is on the pedal ------------------------------

@pytest.fixture
def plugged_in(service, tmp_path, monkeypatch):
    """The service with a pedal mounted on a plain directory."""
    monkeypatch.setattr(config, "MOUNT", tmp_path / "pedal")
    monkeypatch.setattr(pedal, "present", lambda: True)
    monkeypatch.setattr(pedal, "mount", lambda: None)
    monkeypatch.setattr(pedal, "mounted", lambda: True)
    monkeypatch.setattr(pedal, "unmount", lambda: None)
    for n in range(1, config.SLOTS + 1):
        pedal.slot_dir(n).mkdir(parents=True)
    return service


def _plug(service):
    service.pedal_state = "absent"
    service._tick_pedal()
    conftest.drain(service)


def test_removing_a_loop_leaves_a_track_the_database_does_not_know(plugged_in):
    """A BT.WAV with no row still plays. The page used to list only rows and
    loops, so removing the loop beside it made the slot vanish and look as if
    the track had gone too."""
    pedal.track_path(7).write_bytes(b"track")
    pedal.loop_path(7).write_bytes(b"loop")
    _plug(plugged_in)
    assert plugged_in.snapshot()["tracks"] == [7]

    assert plugged_in.delete_loop(7) is True
    conftest.drain(plugged_in)

    assert not pedal.loop_path(7).exists()
    assert plugged_in.snapshot()["tracks"] == [7], \
        "the backing track dropped out of the snapshot with its loop"
    assert pedal.track_path(7).read_bytes() == b"track"


def test_clearing_a_track_the_database_does_not_know_removes_it(plugged_in):
    pedal.track_path(7).write_bytes(b"track")
    _plug(plugged_in)

    plugged_in.clear(7)
    conftest.drain(plugged_in)

    assert not pedal.track_path(7).exists()
    assert plugged_in.snapshot()["tracks"] == []


def test_a_written_track_is_listed(plugged_in):
    _plug(plugged_in)
    h = "a" * 20
    db.library_add(h, "Track", 1.0)
    db.put_slot(3, h, state="staged")
    media.staged_path(h).write_bytes(b"wav")

    plugged_in._do_write(3)

    assert pedal.track_path(3).read_bytes() == b"wav"
    assert plugged_in.snapshot()["tracks"] == [3]


# --- a change to a slot ends its loop ---------------------------------------

def _staged(slot, h, name="Track"):
    db.library_add(h, name, 1.0)
    db.put_slot(slot, h, state="staged")
    media.staged_path(h).write_bytes(name.encode())


def test_writing_a_track_deletes_its_slots_loop(plugged_in):
    """A loop belongs to the track it was recorded over. Left in place it
    plays over the new one."""
    pedal.loop_path(3).write_bytes(b"loop")
    _plug(plugged_in)
    _staged(3, "a" * 20)

    plugged_in._do_write(3)

    assert not pedal.loop_path(3).exists(), "the old loop survived a new track"
    assert plugged_in.snapshot()["loops"] == []


def test_clearing_a_track_deletes_its_loop(plugged_in):
    pedal.track_path(4).write_bytes(b"track")
    pedal.loop_path(4).write_bytes(b"loop")
    _plug(plugged_in)

    plugged_in.clear(4)
    conftest.drain(plugged_in)

    assert not pedal.loop_path(4).exists(), "the loop outlived its track"
    assert plugged_in.snapshot()["loops"] == []


def test_a_swap_deletes_both_loops(plugged_in):
    _staged(1, "a" * 20, "One")
    _staged(2, "b" * 20, "Two")
    for n in (1, 2):
        pedal.loop_path(n).write_bytes(b"loop")
    _plug(plugged_in)

    plugged_in.move(1, 2)
    conftest.drain(plugged_in)

    assert pedal.track_path(1).read_bytes() == b"Two"
    assert pedal.track_path(2).read_bytes() == b"One"
    assert not pedal.loop_path(1).exists() and not pedal.loop_path(2).exists(), \
        "a loop stayed behind under the other slot's track"


def test_a_fill_does_not_write_over_a_track_the_database_does_not_know(
        plugged_in):
    """An unknown BT.WAV has no source here to rewrite it from, so a fill
    skips it the way it skips a loop."""
    pedal.track_path(1).write_bytes(b"track")
    _plug(plugged_in)
    h = "b" * 20
    db.library_add(h, "Track", 1.0)

    plan = plugged_in._plan([h])

    assert plan["assigned"][0]["slot"] == 2, \
        "the fill took the slot holding an unknown backing track"


# --- upload buffers ---------------------------------------------------------

def test_upload_buffers_go_to_the_data_partition(data_tree, monkeypatch):
    """waitress and Flask buffer an upload in Python's temp directory, about
    twice its size in all. The default /tmp is RAM under the read-only root,
    with no swap, so a batch of WAVs could run the Pi out of memory."""
    import tempfile

    import waitress

    from ditto import __main__ as entry

    class FakeService:
        def shutdown(self):
            pass

    def fake_service():
        config.ensure_dirs()
        return FakeService()

    serving_with = []
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.setattr(entry, "Service", fake_service)
    monkeypatch.setattr(entry.signal, "signal", lambda *a: None)
    monkeypatch.setattr(waitress, "serve",
                        lambda *a, **k: serving_with.append(tempfile.gettempdir()))

    assert entry.main([]) == 0
    assert serving_with == [str(config.SPOOL)], \
        "uploads would be buffered in /tmp, which is RAM on the device"
    assert config.SPOOL.is_dir()
