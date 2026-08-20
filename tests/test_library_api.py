"""The library HTTP surface: listing, rename, delete, assign, audition.

These run against a real Service on a throwaway data tree rather than the
duck-typed FakeService in test_web_loops, because the interesting behaviour
(refusing to delete a track a slot still holds, the collector's view of what is
still referenced) lives in the service, not the route. The `service`, `app` and
`client` fixtures come from conftest.
"""

import io
import threading

import pytest

from ditto import config, core, db

H1 = "aaaaaaaaaaaaaaaaaaaa"
H2 = "bbbbbbbbbbbbbbbbbbbb"


def seed(h, name="Track", duration=120.0, body=b"ID3 pretend audio"):
    """Put a track in the library with a source file behind it, without going
    through ffprobe."""
    db.library_add(h, name, duration)
    (config.SOURCES / f"{h}.mp3").write_bytes(body)


# --- listing ----------------------------------------------------------------

def test_library_lists_tracks(client):
    seed(H1, "Blue Bossa")
    seed(H2, "Autumn Leaves")

    rv = client.get("/api/library")

    assert rv.status_code == 200
    assert {r["name"] for r in rv.get_json()} == {"Blue Bossa", "Autumn Leaves"}


def test_library_is_empty_to_start(client):
    assert client.get("/api/library").get_json() == []


def test_the_snapshot_does_not_carry_the_library(client):
    """It rides the SSE stream several times a second during a conversion. The
    library belongs on its own endpoint."""
    seed(H1)
    assert "library" not in client.get("/api/state").get_json()


# --- rename -----------------------------------------------------------------

def test_rename_changes_the_name(client):
    seed(H1, "Before")

    rv = client.patch(f"/api/library/{H1}", json={"name": "After"})

    assert rv.status_code == 200
    assert rv.get_json()["name"] == "After"


def test_rename_reaches_the_slot_list(client):
    """One row changes; the slot list, the grid tooltips and the printed set
    list all read through to it."""
    seed(H1, "Before")
    db.put_slot(4, H1, state="synced")

    client.patch(f"/api/library/{H1}", json={"name": "After"})

    slots = client.get("/api/state").get_json()["slots"]
    assert [s["display_name"] for s in slots] == ["After"]


def test_rename_rejects_an_empty_name(client):
    seed(H1, "Before")
    rv = client.patch(f"/api/library/{H1}", json={"name": "   "})
    assert rv.status_code == 400
    assert db.library_get(H1)["name"] == "Before"


def test_rename_rejects_an_overlong_name(client):
    seed(H1, "Before")
    rv = client.patch(f"/api/library/{H1}", json={"name": "x" * 500})
    assert rv.status_code == 400


def test_rename_of_an_unknown_track_is_404(client):
    assert client.patch(f"/api/library/{H1}", json={"name": "X"}).status_code == 404


def test_rename_of_a_malformed_hash_is_404(client):
    rv = client.patch("/api/library/not-a-hash", json={"name": "X"})
    assert rv.status_code == 404


# --- delete -----------------------------------------------------------------

def test_delete_removes_an_unused_track(client):
    seed(H1)

    rv = client.delete(f"/api/library/{H1}")

    assert rv.status_code == 200
    assert not db.hash_in_library(H1)


def test_delete_refuses_while_a_slot_holds_the_track(client):
    """The one operation that can pull a file out from under a slot."""
    seed(H1)
    db.put_slot(3, H1, state="synced")
    db.put_slot(9, H1, state="synced")

    rv = client.delete(f"/api/library/{H1}")

    assert rv.status_code == 409
    assert rv.get_json()["slots"] == [3, 9]
    assert db.hash_in_library(H1), "the refusal must not delete anything"
    assert db.get_slot(3) is not None


def test_delete_with_force_clears_the_slots_first(client):
    seed(H1)
    db.put_slot(3, H1, state="synced")

    rv = client.delete(f"/api/library/{H1}?force")

    assert rv.status_code == 200
    assert rv.get_json()["cleared"] == [3]
    assert db.get_slot(3) is None
    assert not db.hash_in_library(H1)


def test_delete_of_an_unknown_track_is_404(client):
    assert client.delete(f"/api/library/{H1}").status_code == 404


# --- assign -----------------------------------------------------------------

def test_assign_puts_a_library_track_into_a_slot(client):
    """The point of the library: no re-upload."""
    seed(H1, "Blue Bossa")

    rv = client.post("/api/slots/7/assign", json={"hash": H1})

    assert rv.status_code == 201
    row = db.get_slot(7)
    assert row["source_hash"] == H1
    assert row["display_name"] == "Blue Bossa"


def test_assign_of_an_unknown_track_is_404(client):
    assert client.post("/api/slots/7/assign",
                       json={"hash": H1}).status_code == 404


def test_assign_of_a_malformed_hash_is_404(client):
    rv = client.post("/api/slots/7/assign", json={"hash": "../../etc/passwd"})
    assert rv.status_code == 404


def test_assign_to_an_out_of_range_slot_is_400(client):
    seed(H1)
    assert client.post("/api/slots/200/assign",
                       json={"hash": H1}).status_code == 400


def test_assign_over_an_occupied_slot_keeps_an_undo(client):
    seed(H1, "First")
    seed(H2, "Second")
    client.post("/api/slots/5/assign", json={"hash": H1})

    client.post("/api/slots/5/assign", json={"hash": H2})

    assert db.get_slot(5)["source_hash"] == H2
    assert [t["display_name"] for t in db.trash_items()] == ["First"]


# --- audition ---------------------------------------------------------------

def test_audio_streams_the_source(client):
    seed(H1, body=b"0123456789")

    rv = client.get(f"/api/library/{H1}/audio")

    assert rv.status_code == 200
    assert rv.data == b"0123456789"
    assert rv.headers["Content-Type"].startswith("audio/mpeg")


def test_audio_supports_range_so_the_player_can_seek(client):
    seed(H1, body=b"0123456789")

    rv = client.get(f"/api/library/{H1}/audio", headers={"Range": "bytes=2-5"})

    assert rv.status_code == 206
    assert rv.data == b"2345"
    assert rv.headers["Content-Range"] == "bytes 2-5/10"


def test_audio_rejects_an_unsatisfiable_range(client):
    seed(H1, body=b"0123456789")
    rv = client.get(f"/api/library/{H1}/audio",
                    headers={"Range": "bytes=500-600"})
    assert rv.status_code == 416


def test_audio_is_cacheable_because_the_url_is_content_addressed(client):
    seed(H1)
    rv = client.get(f"/api/library/{H1}/audio")
    assert "immutable" in rv.headers["Cache-Control"]


def test_audio_of_an_unknown_track_is_404(client):
    assert client.get(f"/api/library/{H1}/audio").status_code == 404


@pytest.mark.parametrize("bad", [
    "..%2f..%2fetc%2fpasswd",
    "*",
    "aaaaaaaaaaaaaaaaaaa[",      # a glob class, not a traversal
    "aaaaaaaaaaaaaaaaaaa%3F",    # encoded: a bare ? would start the query string
    "AAAAAAAAAAAAAAAAAAAA",      # uppercase: our hashes are lowercase hex
    "aaaaaaaaaaaaaaaaaaaaa",     # 21 chars
])
def test_audio_rejects_anything_that_is_not_a_hash(client, bad):
    """Two gates, and the regex is the first. It matters because _source_for
    globs sources/{h}.*: a wildcard reaching that glob would match some other
    library file, which path-containment checking would not catch. The library
    lookup behind it would refuse these anyway — that redundancy is the point."""
    seed(H1)
    rv = client.get(f"/api/library/{bad}/audio")
    assert rv.status_code == 404


def test_audio_is_unreachable_once_the_track_is_deleted(client):
    """Bytes can outlive the row by up to one collector pass. The row is the
    gate, not the file."""
    seed(H1)
    db.library_delete(H1)

    assert client.get(f"/api/library/{H1}/audio").status_code == 404


# --- ingest -----------------------------------------------------------------

def test_library_ingest_rejects_a_non_audio_file(client):
    rv = client.post("/api/library", data={
        "file": (__import__("io").BytesIO(b"not audio"), "notes.txt")},
        content_type="multipart/form-data")

    assert rv.status_code == 201
    body = rv.get_json()
    assert body["added"] == []
    assert body["errors"][0]["error"] == "not an audio file"


def test_library_ingest_needs_a_file(client):
    rv = client.post("/api/library", data={}, content_type="multipart/form-data")
    assert rv.status_code == 400


# --- cross-site guard -------------------------------------------------------

@pytest.mark.parametrize("method,path", [
    ("patch", f"/api/library/{H1}"),
    ("delete", f"/api/library/{H1}"),
    ("post", "/api/slots/7/assign"),
    ("post", "/api/library"),
])
def test_library_mutations_are_covered_by_the_cross_site_guard(client, method,
                                                               path):
    rv = getattr(client, method)(path, headers={"Sec-Fetch-Site": "cross-site"})
    assert rv.status_code == 403


# --- front-end assets -------------------------------------------------------

@pytest.mark.parametrize("name,kind", [("app.js", "javascript"),
                                       ("app.css", "css")])
def test_assets_are_served(client, name, kind):
    rv = client.get(f"/static/{name}")
    assert rv.status_code == 200
    assert kind in rv.headers["Content-Type"]
    assert rv.headers["Content-Type"].count("charset") == 1


def test_assets_must_revalidate(client):
    """An over-the-air update must never leave a browser running yesterday's
    app.js against today's API."""
    rv = client.get("/static/app.js")
    assert rv.headers["Cache-Control"] == "no-cache"
    assert rv.headers.get("ETag")


def test_an_unchanged_asset_costs_a_304(client):
    """"Revalidate", not "don't cache" — the body only crosses the wire when it
    has actually changed."""
    etag = client.get("/static/app.js").headers["ETag"]
    rv = client.get("/static/app.js", headers={"If-None-Match": etag})
    assert rv.status_code == 304


def test_the_page_itself_must_revalidate(client):
    assert client.get("/").headers["Cache-Control"] == "no-cache"


@pytest.mark.parametrize("name", ["index.html", "db.py", "app.js.map"])
def test_only_the_named_assets_are_reachable(client, name):
    """An allowlist, not a directory route: a stray file in static/ is never
    served from here.

    index.html is the case that actually exercises the allowlist — it is a real
    file sitting in static/, deliberately absent from ASSETS because it is
    served from "/" instead. The others never reach the check; routing rejects
    them first, which is why this asserts 404 exactly rather than accepting a
    redirect and calling it proof.
    """
    assert client.get(f"/static/{name}").status_code == 404


def test_the_allowlisted_assets_really_are_served(client):
    """The other half: a 404 for everything would satisfy the test above."""
    for name in ("app.js", "app.css"):
        assert client.get(f"/static/{name}").status_code == 200


def test_audition_is_a_safe_method_and_needs_no_guard(client):
    """A GET changes nothing, and the <audio> element can't set headers."""
    seed(H1)
    rv = client.get(f"/api/library/{H1}/audio",
                    headers={"Sec-Fetch-Site": "cross-site"})
    assert rv.status_code == 200


# --- shutdown ---------------------------------------------------------------

def test_library_operations_that_touch_the_pedal_stop_once_ending(client, service):
    """assign and forget queue pedal work, so they refuse during shutdown like
    every other pedal operation — otherwise the API answers 201 for a track
    that poweroff will discard."""
    seed(H1)
    db.put_slot(3, H1, state="synced")
    service.ending = True

    assert client.post("/api/slots/7/assign", json={"hash": H1}).status_code == 503
    assert client.delete(f"/api/library/{H1}?force").status_code == 503
    assert db.get_slot(3) is not None, "a refused delete must not clear slots"
    assert db.hash_in_library(H1)


def test_renaming_still_works_while_ending(client, service):
    """A rename touches one database row and never the pedal, so there is no
    reason to refuse it."""
    seed(H1, "Before")
    service.ending = True

    rv = client.patch(f"/api/library/{H1}", json={"name": "After"})

    assert rv.status_code == 200
    assert db.library_get(H1)["name"] == "After"


# --- malformed JSON bodies --------------------------------------------------

@pytest.mark.parametrize("body", [
    {"name": 42},          # right key, wrong type — used to reach .strip()
    {"name": None},
    {"name": ["a"]},
    [1, 2, 3],             # not an object at all
    "just a string",
])
def test_rename_rejects_a_body_it_cannot_use(client, body):
    """A client can send any JSON. None of it should reach .strip() and come
    back as a 500."""
    seed(H1, "Before")
    rv = client.patch(f"/api/library/{H1}", json=body)
    assert rv.status_code == 400
    assert db.library_get(H1)["name"] == "Before"


@pytest.mark.parametrize("body", [
    {"hash": 42},          # used to reach the hash regex and raise
    {"hash": None},
    {"hash": []},
    [1, 2, 3],
    "just a string",
])
def test_assign_rejects_a_body_it_cannot_use(client, body):
    seed(H1)
    rv = client.post("/api/slots/7/assign", json=body)
    assert rv.status_code == 404
    assert db.get_slot(7) is None


def test_forget_reports_the_slots_it_cleared_even_if_the_row_had_gone(client):
    """The slots really were cleared. Pairing a failure with an empty list would
    read as "nothing happened"."""
    seed(H1)
    db.put_slot(3, H1, state="synced")
    # Delete the row underneath, leaving the slot pointing at nothing — the
    # shape a concurrent delete produces.
    db.library_delete(H1)

    rv = client.delete(f"/api/library/{H1}?force")

    assert rv.status_code == 404
    assert rv.get_json()["cleared"] == [3]
    assert db.get_slot(3) is None, "the slot really was cleared"


# --- ingest failure must not leave a row with no audio ----------------------

def _service_of(app):
    """The Service the app was built around, for tests that drive it directly."""
    return app.view_functions["state"].__closure__[0].cell_contents


def test_a_failed_store_retracts_the_row_it_created(app, monkeypatch, tmp_path):
    """_store_source raises when it cannot confirm the bytes. Nothing collects
    a library row, so one left behind would sit in the list forever — failing
    to audition and reporting "source file missing" on every assignment."""
    svc = _service_of(app)
    src = tmp_path / "upload.mp3"
    src.write_bytes(b"pretend audio")

    monkeypatch.setattr(core.media, "probe",
                        lambda p: core.media.AudioInfo("mp3", 44100, 2, 90.0))
    monkeypatch.setattr(core.media, "file_hash", lambda p: H1)
    monkeypatch.setattr(core.Service, "_store_source",
                        staticmethod(lambda t, s: (_ for _ in ()).throw(
                            OSError("no space left on device"))))

    with pytest.raises(OSError):
        svc.add_to_library(src, "Doomed")

    assert db.library_get(H1) is None, "a row with no audio behind it"
    assert client_library_empty(app)


def client_library_empty(app):
    return app.test_client().get("/api/library").get_json() == []


def test_a_failed_store_leaves_an_established_row_alone(app, monkeypatch, tmp_path):
    """library_add is DO NOTHING on conflict, so re-uploading a track already in
    the library must not let a failure here delete the established row — whose
    file is still good, and is exactly why the write was skipped."""
    svc = _service_of(app)
    seed(H1, "Already here")
    src = tmp_path / "upload.mp3"
    src.write_bytes(b"pretend audio")

    monkeypatch.setattr(core.media, "probe",
                        lambda p: core.media.AudioInfo("mp3", 44100, 2, 90.0))
    monkeypatch.setattr(core.media, "file_hash", lambda p: H1)
    boom = staticmethod(lambda t, s: (_ for _ in ()).throw(OSError("nope")))
    monkeypatch.setattr(core.Service, "_store_source", boom)

    # The stored file already exists, so this returns without calling through.
    svc.add_to_library(src, "Second upload")

    assert db.library_get(H1)["name"] == "Already here"


def test_library_ingest_is_refused_once_the_session_is_ending(app, monkeypatch,
                                                              tmp_path):
    """Ingest has to be admitted like every other mutation. Without it,
    end_session could begin the halt while the source file was still being
    written, and this would still report success."""
    svc = _service_of(app)
    src = tmp_path / "upload.mp3"
    src.write_bytes(b"pretend audio")
    monkeypatch.setattr(core.media, "probe",
                        lambda p: core.media.AudioInfo("mp3", 44100, 2, 90.0))
    monkeypatch.setattr(core.media, "file_hash", lambda p: H1)

    svc.ending = True
    try:
        with pytest.raises(core.ShuttingDown):
            svc.add_to_library(src, "Too late")
    finally:
        svc.ending = False

    assert db.library_get(H1) is None


def test_upload_holds_one_admission_over_the_whole_mutation(app, monkeypatch,
                                                            tmp_path):
    """The row, the bytes and the slot assignment have to land as one step, or
    a concurrent forced forget can delete the row in between and leave a slot
    pointing at a hash with no library entry."""
    svc = _service_of(app)
    src = tmp_path / "upload.mp3"
    src.write_bytes(b"pretend audio")
    monkeypatch.setattr(core.media, "probe",
                        lambda p: core.media.AudioInfo("mp3", 44100, 2, 90.0))
    monkeypatch.setattr(core.media, "file_hash", lambda p: H1)

    held = []
    real_place = core.Service._place_source

    def watch(self, h, tmp, stored, inserted):
        # The admission is an RLock; if it is held, this thread already owns it.
        held.append(svc._admit._is_owned())
        return real_place(self, h, tmp, stored, inserted)

    monkeypatch.setattr(core.Service, "_place_source", watch)
    svc.upload(5, src, "Track")

    assert held == [True], "the source was stored outside the admission"
    row = db.get_slot(5)
    assert row["source_hash"] == H1
    assert db.hash_in_library(H1), "the slot must never outlive its library row"


def test_assign_reads_the_row_back_under_the_admission(app, monkeypatch):
    """A concurrent forced forget can clear the slot the instant the lock is
    released. Reading outside the block would return None for an assignment
    that did happen, which the route maps to a 404."""
    svc = _service_of(app)
    seed(H1, "Track")

    observed = []
    real_get = db.get_slot
    caller = threading.current_thread()

    def watch(slot):
        # This thread only. The worker picks up the convert job and calls
        # get_slot too, and it never holds the admission — counting that would
        # make the assertion fail regardless of what assign() does.
        if threading.current_thread() is caller:
            observed.append(svc._admit._is_owned())
        return real_get(slot)

    monkeypatch.setattr(db, "get_slot", watch)
    row = svc.assign(7, H1)

    assert row is not None and row["source_hash"] == H1
    assert observed and observed[-1] is True, \
        "the slot row was read after the admission was released"


# --- a batch must not lose what already landed -------------------------------

def _audio(name="track.mp3", body=b"pretend audio"):
    return (io.BytesIO(body), name)


def test_a_batch_reports_the_files_that_landed_when_one_cannot_be_stored(
        app, client, monkeypatch):
    """_store_source raises when it cannot confirm bytes on the card — the
    expected failure on this device, and a set-list drop is exactly when it
    happens. The files already committed and queued must still be reported, or
    the UI cannot tell what landed."""
    svc = _service_of(app)
    monkeypatch.setattr(core.media, "probe",
                        lambda p: core.media.AudioInfo("mp3", 44100, 2, 90.0))
    hashes = iter([f"{i:020d}" for i in range(10)])
    monkeypatch.setattr(core.media, "file_hash", lambda p: next(hashes))

    calls = {"n": 0}
    real_store = core.Service._store_source

    def fail_on_the_third(tmp_path, stored):
        calls["n"] += 1
        if calls["n"] == 3:
            tmp_path.unlink(missing_ok=True)
            raise OSError("no space left on device")
        return real_store(tmp_path, stored)

    monkeypatch.setattr(core.Service, "_store_source",
                        staticmethod(fail_on_the_third))

    rv = client.post("/api/library", content_type="multipart/form-data", data={
        "file": [_audio("a.mp3"), _audio("b.mp3"), _audio("c.mp3"),
                 _audio("d.mp3")]})

    assert rv.status_code == 201
    body = rv.get_json()
    assert len(body["added"]) == 3, "files that landed were thrown away"
    assert len(body["errors"]) == 1
    assert body["errors"][0]["name"] == "c.mp3"
    assert "could not be saved" in body["errors"][0]["error"]


def test_a_batch_stops_and_reports_partials_when_the_device_starts_halting(
        app, client, monkeypatch):
    """ShuttingDown is different from a per-file failure: nothing further can
    land, so the batch stops — but what already landed is still reported."""
    svc = _service_of(app)
    monkeypatch.setattr(core.media, "probe",
                        lambda p: core.media.AudioInfo("mp3", 44100, 2, 90.0))
    hashes = iter([f"{i:020d}" for i in range(10)])
    monkeypatch.setattr(core.media, "file_hash", lambda p: next(hashes))

    seen = {"n": 0}
    real_add = core.Service.add_to_library

    def halt_after_two(self, tmp_path, display_name):
        seen["n"] += 1
        if seen["n"] > 2:
            self.ending = True
        return real_add(self, tmp_path, display_name)

    monkeypatch.setattr(core.Service, "add_to_library", halt_after_two)
    try:
        rv = client.post("/api/library", content_type="multipart/form-data",
                         data={"file": [_audio("a.mp3"), _audio("b.mp3"),
                                        _audio("c.mp3"), _audio("d.mp3")]})
        assert rv.status_code == 503
        assert len(rv.get_json()["added"]) == 2, "partials were discarded"
    finally:
        svc.ending = False


def test_a_single_upload_that_cannot_be_stored_is_a_500_not_a_400(
        app, client, monkeypatch):
    """A card that will not take the bytes is our failure, not the client's."""
    monkeypatch.setattr(core.media, "probe",
                        lambda p: core.media.AudioInfo("mp3", 44100, 2, 90.0))
    monkeypatch.setattr(core.media, "file_hash", lambda p: H1)
    monkeypatch.setattr(core.Service, "_store_source", staticmethod(
        lambda t, s: (_ for _ in ()).throw(OSError("no space left on device"))))

    rv = client.post("/api/slots/4", content_type="multipart/form-data",
                     data={"file": _audio()})

    assert rv.status_code == 500
    assert "could not be saved" in rv.get_json()["error"]


def test_a_single_upload_of_a_bad_file_is_still_a_400(app, client, monkeypatch):
    monkeypatch.setattr(core.media, "probe", lambda p: None)
    rv = client.post("/api/slots/4", content_type="multipart/form-data",
                     data={"file": _audio()})
    assert rv.status_code == 400
