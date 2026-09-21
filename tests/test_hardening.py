"""Durable ingest, the boot sweep, newest-wins SSE, and shutdown.

Fixtures come from conftest: `data_tree` for the tests that only touch files,
`service` for the ones that need the worker.
"""

import json
import os
import pathlib
import queue
import threading

import conftest
import pytest

from ditto import config, core, db, pedal, update

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


def test_a_loop_delete_does_not_wait_for_a_running_job(service):
    """The worker holds the job lock for a whole write, minutes for a long
    track. A request thread that queues behind it looks like a hung page."""
    service._loops = frozenset({5})
    result = []
    with service._job_lock:
        t = threading.Thread(target=lambda: result.append(service.delete_loop(5)))
        t.start()
        t.join(timeout=2.0)
    assert result == [None], "delete_loop waited for the job lock"


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


def test_the_suite_cannot_restart_the_machine():
    """A successful update ends in `sudo -n systemctl start ditto-restart`, and
    install.sh grants exactly that. conftest replaces the call; this is what
    says so out loud, so the seam cannot be removed silently."""
    before = len(conftest.sudo_attempts)
    argv = ["sudo", "-n", "/usr/bin/systemctl", "start", "--no-block",
            config.RESTART_SERVICE]

    result = update.subprocess.run(argv, check=False)

    assert result.returncode == 0, "the guard should stand in for the real call"
    assert conftest.sudo_attempts[before:] == [argv]


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
