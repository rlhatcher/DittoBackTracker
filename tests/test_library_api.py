"""The library HTTP surface: listing, rename, delete, assign, audition.

These run against a real Service on a throwaway data tree rather than the
duck-typed FakeService in test_web_loops, because the interesting behaviour
(refusing to delete a track a slot still holds, the collector's view of what is
still referenced) lives in the service, not the route. The `service`, `app` and
`client` fixtures come from conftest.
"""

import hashlib
import io
import os
import threading
import time

import pytest

from ditto import config, core, db, web

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


@pytest.mark.parametrize("name", sorted(web.ASSETS))
def test_the_allowlisted_assets_really_are_served(client, name):
    """The other half: a 404 for everything would satisfy the test above.

    Driven off ASSETS itself rather than a copy of the list, so adding an entry
    without shipping the file fails here instead of in a browser.
    """
    rv = client.get(f"/static/{name}")
    assert rv.status_code == 200, f"{name} is allowlisted but not on disk"
    assert rv.headers["Content-Type"].startswith(web.ASSETS[name])


def test_the_webfont_survives_being_checked_out(client):
    """.gitattributes says `* text=auto`, which would LF-normalise a woff2 and
    corrupt it. Nothing about that failure is visible server-side — the file is
    still served, still the right length-ish, and no browser will parse it. The
    magic number is the cheapest thing that actually notices.
    """
    body = client.get("/static/archivo-latin.woff2").data
    assert body[:4] == b"wOF2", "the font is not woff2 — check .gitattributes"


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


# --- folders ----------------------------------------------------------------

def mkfolder(client, name, parent=None):
    rv = client.post("/api/folders", json={"name": name, "parent_id": parent})
    assert rv.status_code == 201, rv.get_json()
    return rv.get_json()


def test_folders_start_empty(client):
    assert client.get("/api/folders").get_json() == []


def test_a_new_folder_comes_back_with_its_id(client):
    body = mkfolder(client, "Standards")
    assert body["name"] == "Standards"
    assert body["parent_id"] is None
    assert body["id"] > 0


def test_a_folder_can_be_created_inside_another(client):
    top = mkfolder(client, "Standards")
    kid = mkfolder(client, "Ballads", top["id"])
    assert kid["parent_id"] == top["id"]
    assert len(client.get("/api/folders").get_json()) == 2


def test_creating_a_folder_under_an_unknown_parent_is_404(client):
    rv = client.post("/api/folders", json={"name": "Orphan", "parent_id": 999})
    assert rv.status_code == 404
    assert client.get("/api/folders").get_json() == []


@pytest.mark.parametrize("body", [
    {}, {"name": ""}, {"name": "   "}, {"name": 42}, {"name": None},
    {"name": "x" * 201},
])
def test_a_folder_name_must_be_usable(client, body):
    assert client.post("/api/folders", json=body).status_code == 400


@pytest.mark.parametrize("parent", ["1", 1.5, True, [], {}])
def test_a_parent_id_that_is_not_a_folder_id_is_400(client, parent):
    """True is an int in Python and would quietly mean folder 1."""
    rv = client.post("/api/folders", json={"name": "F", "parent_id": parent})
    assert rv.status_code == 400


def test_folders_nest_only_so_deep(client):
    parent = None
    for i in range(config.MAX_FOLDER_DEPTH):
        parent = mkfolder(client, f"L{i}", parent)["id"]
    rv = client.post("/api/folders", json={"name": "too deep", "parent_id": parent})
    assert rv.status_code == 400
    assert "nest" in rv.get_json()["error"]


def test_renaming_a_folder_returns_the_updated_row(client):
    f = mkfolder(client, "Standards")
    rv = client.patch(f"/api/folders/{f['id']}", json={"name": "Set list"})
    assert rv.status_code == 200
    assert rv.get_json()["name"] == "Set list"


def test_renaming_an_unknown_folder_is_404(client):
    assert client.patch("/api/folders/999", json={"name": "X"}).status_code == 404


def test_a_patch_with_nothing_to_change_is_400(client):
    f = mkfolder(client, "Standards")
    assert client.patch(f"/api/folders/{f['id']}", json={}).status_code == 400


def test_a_patch_can_rename_and_move_in_one_call(client):
    top = mkfolder(client, "Top")
    f = mkfolder(client, "Standards")

    rv = client.patch(f"/api/folders/{f['id']}",
                      json={"name": "Set list", "parent_id": top["id"]})

    assert rv.status_code == 200
    body = rv.get_json()
    assert body["name"] == "Set list"
    assert body["parent_id"] == top["id"]


def test_a_folder_can_be_moved_back_to_the_top_level(client):
    """null and absent mean different things here: move to the top, or leave it
    where it is."""
    top = mkfolder(client, "Top")
    kid = mkfolder(client, "Kid", top["id"])

    rv = client.patch(f"/api/folders/{kid['id']}", json={"parent_id": None})

    assert rv.status_code == 200
    assert rv.get_json()["parent_id"] is None


def test_moving_a_folder_into_its_own_subtree_is_400_and_changes_nothing(client):
    top = mkfolder(client, "Top")
    kid = mkfolder(client, "Kid", top["id"])

    rv = client.patch(f"/api/folders/{top['id']}", json={"parent_id": kid["id"]})

    assert rv.status_code == 400
    assert "subtree" in rv.get_json()["error"]
    assert client.get("/api/folders").get_json()[0]["parent_id"] is None


def test_deleting_an_empty_folder_succeeds(client):
    f = mkfolder(client, "Empty")
    rv = client.delete(f"/api/folders/{f['id']}")
    assert rv.status_code == 200
    assert rv.get_json()["ok"] is True
    assert client.get("/api/folders").get_json() == []


def test_deleting_an_unknown_folder_is_404(client):
    assert client.delete("/api/folders/999").status_code == 404


def test_deleting_a_folder_that_holds_tracks_is_409_and_changes_nothing(client):
    f = mkfolder(client, "Standards")
    seed(H1)
    db.library_set_folder(H1, f["id"])

    rv = client.delete(f"/api/folders/{f['id']}")

    assert rv.status_code == 409
    body = rv.get_json()
    assert body["error"] == "not empty" and body["tracks"] == 1
    assert client.get("/api/folders").get_json() != []


def test_a_forced_delete_promotes_the_contents_and_says_where(client):
    top = mkfolder(client, "Top")
    mid = mkfolder(client, "Mid", top["id"])
    leaf = mkfolder(client, "Leaf", mid["id"])
    seed(H1)
    db.library_set_folder(H1, mid["id"])

    rv = client.delete(f"/api/folders/{mid['id']}?force")

    assert rv.status_code == 200
    body = rv.get_json()
    assert body["to"] == top["id"]
    assert body["promoted"] == {"folders": [leaf["id"]], "tracks": 1}
    assert db.library_get(H1)["folder_id"] == top["id"]


def test_a_forced_folder_delete_leaves_the_source_file_alone(client, service):
    """The one that would destroy something. A library row is the only thing
    keeping its audio in sources/, so this runs the collector afterwards rather
    than trusting that the row is still there."""
    f = mkfolder(client, "Standards")
    seed(H1, "Blue Bossa")
    db.library_set_folder(H1, f["id"])
    source = config.SOURCES / f"{H1}.mp3"
    assert source.exists()

    client.delete(f"/api/folders/{f['id']}?force")
    # The collector spares recent files, so age it past the grace period first.
    old = time.time() - config.GC_GRACE_SECS - 60
    os.utime(source, (old, old))
    service._gc()

    assert source.exists(), "the folder delete released the audio"
    assert db.library_get(H1) is not None


def test_a_library_row_carries_its_folder(client):
    f = mkfolder(client, "Standards")
    seed(H1)
    db.library_set_folder(H1, f["id"])

    row = client.get("/api/library").get_json()[0]

    assert row["folder_id"] == f["id"]
    assert row["position"] == 0


def test_the_library_still_lists_newest_first(client):
    """Pinned because the folder work must not reorder it."""
    seed(H1, "Older")
    seed(H2, "Newer")
    assert [r["name"] for r in client.get("/api/library").get_json()] == \
        ["Newer", "Older"]


def test_the_snapshot_does_not_carry_the_folder_tree(client):
    """It is pushed several times a second during a conversion and has to stay
    small. Same reasoning that keeps the library out of it."""
    mkfolder(client, "Standards")
    snap = client.get("/api/state").get_json()
    assert "folders" not in snap


@pytest.mark.parametrize("method,path", [
    ("post", "/api/folders"),
    ("patch", "/api/folders/1"),
    ("delete", "/api/folders/1"),
])
def test_folder_mutations_are_covered_by_the_cross_site_guard(client, method, path):
    rv = getattr(client, method)(path, headers={"Origin": "http://evil.example"})
    assert rv.status_code == 403


# --- filing tracks ----------------------------------------------------------

def test_a_patch_can_file_a_track(client):
    f = mkfolder(client, "Standards")
    seed(H1, "Blue Bossa")

    rv = client.patch(f"/api/library/{H1}", json={"folder_id": f["id"]})

    assert rv.status_code == 200
    assert rv.get_json()["folder_id"] == f["id"]


def test_filing_a_track_does_not_rename_it(client):
    f = mkfolder(client, "Standards")
    seed(H1, "Blue Bossa")

    client.patch(f"/api/library/{H1}", json={"folder_id": f["id"]})

    assert db.library_get(H1)["name"] == "Blue Bossa"


def test_a_patch_can_rename_and_file_in_one_call(client):
    f = mkfolder(client, "Standards")
    seed(H1, "Before")

    rv = client.patch(f"/api/library/{H1}",
                      json={"name": "After", "folder_id": f["id"]})

    body = rv.get_json()
    assert body["name"] == "After"
    assert body["folder_id"] == f["id"]


def test_a_track_can_be_filed_back_to_the_top_level(client):
    f = mkfolder(client, "Standards")
    seed(H1)
    db.library_set_folder(H1, f["id"])

    rv = client.patch(f"/api/library/{H1}", json={"folder_id": None})

    assert rv.status_code == 200
    assert rv.get_json()["folder_id"] is None


def test_filing_into_an_unknown_folder_is_404_and_changes_nothing(client):
    seed(H1, "Before")

    rv = client.patch(f"/api/library/{H1}",
                      json={"name": "After", "folder_id": 999})

    assert rv.status_code == 404
    assert db.library_get(H1)["name"] == "Before", "the rename must not survive"


@pytest.mark.parametrize("folder", ["1", 1.5, True, [], {}])
def test_a_folder_id_that_is_not_a_folder_id_is_400(client, folder):
    seed(H1)
    rv = client.patch(f"/api/library/{H1}", json={"folder_id": folder})
    assert rv.status_code == 400


def test_a_patch_with_neither_a_name_nor_a_folder_is_400(client):
    seed(H1)
    assert client.patch(f"/api/library/{H1}", json={}).status_code == 400


def test_filing_still_works_while_the_device_is_ending(client, service):
    """One row changes and the pedal is never touched, so this is not work the
    shutdown has to refuse — the same reasoning as a rename."""
    f = mkfolder(client, "Standards")
    seed(H1)
    service.ending = True

    rv = client.patch(f"/api/library/{H1}", json={"folder_id": f["id"]})

    assert rv.status_code == 200


def upload(client, path, name="t.mp3", **form):
    return client.post(path, data={"file": (io.BytesIO(b"ID3 pretend"), name), **form},
                       content_type="multipart/form-data")


def test_a_library_upload_files_its_track_in_the_named_folder(client, monkeypatch):
    monkeypatch.setattr(core.media, "probe", lambda p: core.media.AudioInfo(
        "mp3", 44100, 2, 12.0))
    f = mkfolder(client, "Standards")

    rv = upload(client, "/api/library", folder_id=str(f["id"]))

    assert rv.status_code == 201
    added = rv.get_json()["added"]
    assert len(added) == 1
    assert db.library_get(added[0]["source_hash"])["folder_id"] == f["id"]


def test_an_upload_with_no_folder_lands_at_the_top_level(client, monkeypatch):
    monkeypatch.setattr(core.media, "probe", lambda p: core.media.AudioInfo(
        "mp3", 44100, 2, 12.0))

    rv = upload(client, "/api/library")

    h = rv.get_json()["added"][0]["source_hash"]
    assert db.library_get(h)["folder_id"] is None


def test_a_slot_upload_files_its_track_too(client, monkeypatch):
    """The track reaches the library either way, so it should be filable either
    way."""
    monkeypatch.setattr(core.media, "probe", lambda p: core.media.AudioInfo(
        "mp3", 44100, 2, 12.0))
    f = mkfolder(client, "Standards")

    rv = upload(client, "/api/slots/3", folder_id=str(f["id"]))

    assert rv.status_code == 201
    assert db.library_get(rv.get_json()["source_hash"])["folder_id"] == f["id"]


@pytest.mark.parametrize("path", ["/api/library", "/api/upload", "/api/slots/3"])
def test_an_upload_to_an_unknown_folder_lands_nothing(client, monkeypatch, path):
    """The whole request fails, not each file in it. Every file would fail the
    same way, and a half-applied batch is worse than a refused one.

    probe is stubbed so the file *would* be accepted. Without that, nothing
    lands because the bytes are not audio, and the test passes whether the
    folder is checked before the ingest or after it.
    """
    monkeypatch.setattr(core.media, "probe", lambda p: core.media.AudioInfo(
        "mp3", 44100, 2, 12.0))

    rv = upload(client, path, folder_id="999")

    assert rv.status_code == 404
    assert db.library_all() == [], "a refused request must not leave a track behind"
    assert db.all_slots() == []


@pytest.mark.parametrize("path", ["/api/library", "/api/upload", "/api/slots/3"])
def test_an_upload_with_a_junk_folder_id_is_400(client, monkeypatch, path):
    monkeypatch.setattr(core.media, "probe", lambda p: core.media.AudioInfo(
        "mp3", 44100, 2, 12.0))

    rv = upload(client, path, folder_id="not-a-number")

    assert rv.status_code == 400
    assert db.library_all() == []


def test_a_rejected_move_does_not_rename_the_folder(client):
    """The 404 arrives after the rename would have been written, if the two were
    separate calls."""
    f = mkfolder(client, "Before")

    rv = client.patch(f"/api/folders/{f['id']}",
                      json={"name": "After", "parent_id": 999})

    assert rv.status_code == 404
    assert db.folder_get(f["id"])["name"] == "Before"


def test_a_cyclic_move_does_not_rename_the_folder(client):
    top = mkfolder(client, "Before")
    kid = mkfolder(client, "Kid", top["id"])

    rv = client.patch(f"/api/folders/{top['id']}",
                      json={"name": "After", "parent_id": kid["id"]})

    assert rv.status_code == 400
    assert db.folder_get(top["id"])["name"] == "Before"


def test_an_upload_reports_the_folder_the_track_is_actually_in(client, monkeypatch):
    """The folder is checked before the file is taken, but a forced delete can
    commit while the upload is being written. The response must describe the
    database rather than the request."""
    monkeypatch.setattr(core.media, "probe", lambda p: core.media.AudioInfo(
        "mp3", 44100, 2, 12.0))
    f = mkfolder(client, "Standards")

    real_set = db.library_set_folder

    def vanish(h, folder_id):
        # the folder goes between the check and the filing
        db.conn().execute("DELETE FROM folders WHERE id=?", (folder_id,))
        db.conn().commit()
        return real_set(h, folder_id)

    monkeypatch.setattr(db, "library_set_folder", vanish)
    rv = upload(client, "/api/library", folder_id=str(f["id"]))

    assert rv.status_code == 201
    row = rv.get_json()["added"][0]
    assert row["folder_id"] is None, "reported a folder the track is not in"
    assert db.library_get(row["source_hash"])["folder_id"] is None


# --- folder assign ----------------------------------------------------------

def fill(client, folder_id, *names):
    """Seed tracks and file them into a folder, in the order given.

    The hash is derived from the name so a test reads the same twice; str.hash
    is salted per process and would make the fixture different every run.
    """
    hashes = []
    for name in names:
        h = hashlib.sha1(name.encode()).hexdigest()[:20]
        seed(h, name)
        assert client.patch(f"/api/library/{h}",
                            json={"folder_id": folder_id}).status_code == 200
        hashes.append(h)
    return hashes


def test_a_folder_fills_consecutive_slots_in_tree_order(client):
    f = mkfolder(client, "Standards")
    fill(client, f["id"], "Autumn Leaves", "Blue Bossa", "Ceora")

    rv = client.post(f"/api/folders/{f['id']}/assign", json={"start": 9})

    assert rv.status_code == 201
    body = rv.get_json()
    assert (body["start"], body["end"]) == (9, 11)
    assert [a["name"] for a in body["assigned"]] == \
        ["Autumn Leaves", "Blue Bossa", "Ceora"]
    assert [db.get_slot(n)["display_name"] for n in (9, 10, 11)] == \
        ["Autumn Leaves", "Blue Bossa", "Ceora"]


def test_a_fill_takes_the_tracks_in_subfolders_too(client):
    """Tree order: a folder's own tracks, then each subfolder expanded."""
    top = mkfolder(client, "Set one")
    sub = mkfolder(client, "Encores", parent=top["id"])
    fill(client, top["id"], "Autumn Leaves")
    fill(client, sub["id"], "Ceora")

    rv = client.post(f"/api/folders/{top['id']}/assign", json={"start": 1})

    assert [a["name"] for a in rv.get_json()["assigned"]] == \
        ["Autumn Leaves", "Ceora"]


def test_a_fill_with_no_start_takes_the_first_slot_with_room(client):
    f = mkfolder(client, "Standards")
    fill(client, f["id"], "Autumn Leaves", "Blue Bossa")
    seed(H1)
    client.post("/api/slots/1/assign", json={"hash": H1})

    body = client.post(f"/api/folders/{f['id']}/assign").get_json()

    assert (body["start"], body["end"]) == (2, 3)


def test_folder_assign_uses_the_devices_own_loop_set_and_not_the_clients(
        client, service):
    """The loop set is scanned at mount and lives only on the device. A client
    working from a snapshot that can be fifteen seconds old would lose a replug
    race, so the skip happens here, under the lock that queues the work."""
    service.pedal_state = "mounted"
    service._loops = frozenset({10})
    f = mkfolder(client, "Standards")
    fill(client, f["id"], "Autumn Leaves", "Blue Bossa")

    body = client.post(f"/api/folders/{f['id']}/assign",
                       json={"start": 9}).get_json()

    assert [a["slot"] for a in body["assigned"]] == [9, 11]
    assert body["skipped_loops"] == [10]
    assert (body["start"], body["end"]) == (9, 11), "the range spans the skip"
    assert body["loops_known"] is True
    assert db.get_slot(10) is None, "wrote over a loop slot"


def test_a_loop_at_the_requested_start_is_reported_as_skipped(client, service):
    service.pedal_state = "mounted"
    service._loops = frozenset({9})
    f = mkfolder(client, "Standards")
    fill(client, f["id"], "Autumn Leaves")

    body = client.get(f"/api/folders/{f['id']}/assign?start=9").get_json()

    assert body["start"] == 10
    assert body["skipped_loops"] == [9], \
        "asking for 09 and getting 10 is only legible with the 09 in here"


def test_folder_assign_cannot_skip_loops_while_the_pedal_is_absent(client,
                                                                   service):
    """Loop presence is only knowable mounted, so an unmounted plan says its
    range is provisional rather than letting a client believe it skipped."""
    assert service.pedal_state == "absent"
    service._loops = frozenset()
    f = mkfolder(client, "Standards")
    fill(client, f["id"], "Autumn Leaves")

    body = client.post(f"/api/folders/{f['id']}/assign",
                       json={"start": 9}).get_json()

    assert body["loops_known"] is False
    assert body["skipped_loops"] == []


def test_the_preview_returns_the_plan_the_assign_then_writes(client):
    f = mkfolder(client, "Standards")
    fill(client, f["id"], "Autumn Leaves", "Blue Bossa")

    preview = client.get(f"/api/folders/{f['id']}/assign?start=4").get_json()
    written = client.post(f"/api/folders/{f['id']}/assign",
                          json={"start": 4}).get_json()

    assert preview.pop("dry_run") is True
    assert written.pop("dry_run") is False
    assert preview == written


def test_the_preview_writes_nothing(client):
    f = mkfolder(client, "Standards")
    fill(client, f["id"], "Autumn Leaves")

    client.get(f"/api/folders/{f['id']}/assign?start=4")

    assert db.all_slots() == []


def test_a_fill_reports_the_tracks_that_did_not_fit(client):
    """Silent truncation would put two tracks on the pedal and lose two."""
    f = mkfolder(client, "Standards")
    fill(client, f["id"], "Autumn Leaves", "Blue Bossa", "Ceora")

    body = client.post(f"/api/folders/{f['id']}/assign",
                       json={"start": config.SLOTS - 1}).get_json()

    assert [a["slot"] for a in body["assigned"]] == \
        [config.SLOTS - 1, config.SLOTS]
    assert [u["name"] for u in body["unplaced"]] == ["Ceora"]
    assert body["unplaced"][0]["error"] == f"no room past slot {config.SLOTS}"


def test_a_fill_with_no_room_at_all_places_nothing(client):
    f = mkfolder(client, "Standards")
    fill(client, f["id"], "Autumn Leaves")
    seed(H1)
    for n in range(1, config.SLOTS + 1):
        db.put_slot(n, H1, state="synced")

    body = client.post(f"/api/folders/{f['id']}/assign").get_json()

    assert body["assigned"] == []
    assert (body["start"], body["end"]) == (None, None)
    assert len(body["unplaced"]) == 1


def test_an_empty_folder_assigns_nothing(client):
    f = mkfolder(client, "Standards")

    body = client.post(f"/api/folders/{f['id']}/assign").get_json()

    assert body["assigned"] == [] and body["unplaced"] == []
    assert (body["start"], body["end"]) == (None, None)


def test_a_fill_is_refused_once_the_device_is_ending(client, service):
    """It queues pedal work, so it stops when every other pedal operation
    does — otherwise the API reports slots that poweroff will discard."""
    f = mkfolder(client, "Standards")
    fill(client, f["id"], "Autumn Leaves", "Blue Bossa")
    service.ending = True

    rv = client.post(f"/api/folders/{f['id']}/assign", json={"start": 9})

    assert rv.status_code == 503
    assert db.all_slots() == []


def test_folder_assign_writes_the_whole_plan_or_none_of_it(client, service,
                                                           monkeypatch):
    """end_session sets `ending` under the admission lock, so holding that lock
    across the whole fill is what stops a shutdown landing between the fourth
    track and the fifth and leaving half a set list on the pedal. Probed with a
    non-blocking acquire from another thread, which is the mechanism itself —
    asserting on the outcome of a race would only sometimes run the race."""
    f = mkfolder(client, "Standards")
    fill(client, f["id"], "Autumn Leaves", "Blue Bossa", "Ceora")
    real = service._assign
    held = []

    def watched(slot, h):
        got = []

        def probe():
            # An RLock is reentrant for its owner, so this has to be asked from
            # a thread that is not the one running the fill.
            ok = service._admit.acquire(blocking=False)
            if ok:
                service._admit.release()
            got.append(ok)

        t = threading.Thread(target=probe)
        t.start()
        t.join()
        held.append(not got[0])
        real(slot, h)

    monkeypatch.setattr(service, "_assign", watched)
    client.post(f"/api/folders/{f['id']}/assign", json={"start": 9})

    assert held == [True, True, True], "the fill let go of the admission"


def test_folder_assign_emits_one_snapshot_for_the_whole_fill(client, service,
                                                             monkeypatch):
    """Nine emits would each rebuild a full snapshot and broadcast 99 slots to
    every subscriber. Counted on the request's own thread — the worker emits on
    its own as the conversions it queued run."""
    f = mkfolder(client, "Standards")
    fill(client, f["id"], "Autumn Leaves", "Blue Bossa", "Ceora")
    caller = threading.current_thread()
    emits = []
    real = service._emit

    def counting():
        if threading.current_thread() is caller:
            emits.append(1)
        real()

    monkeypatch.setattr(service, "_emit", counting)
    client.post(f"/api/folders/{f['id']}/assign", json={"start": 9})

    assert len(emits) == 1


def test_a_fill_that_places_nothing_emits_nothing(client, service, monkeypatch):
    f = mkfolder(client, "Standards")
    caller = threading.current_thread()
    emits = []
    real = service._emit
    monkeypatch.setattr(service, "_emit",
                        lambda: (emits.append(1)
                                 if threading.current_thread() is caller
                                 else None, real())[1])

    client.post(f"/api/folders/{f['id']}/assign")

    assert emits == []


def test_a_fill_over_an_occupied_slot_keeps_an_undo(client):
    seed(H1, "Was here")
    client.post("/api/slots/9/assign", json={"hash": H1})
    f = mkfolder(client, "Standards")
    fill(client, f["id"], "Autumn Leaves")

    client.post(f"/api/folders/{f['id']}/assign", json={"start": 9})

    assert db.get_slot(9)["display_name"] == "Autumn Leaves"
    assert [t["display_name"] for t in db.trash_items()] == ["Was here"]


def test_assigning_an_unknown_folder_is_404(client):
    assert client.post("/api/folders/99/assign").status_code == 404
    assert client.get("/api/folders/99/assign").status_code == 404


@pytest.mark.parametrize("start", ["abc", "", True, 0, 200, -1])
def test_a_start_that_is_not_a_slot_number_is_400(client, start):
    """Absent means "wherever there is room". A start the caller got wrong is a
    different thing and is refused rather than quietly treated as absent."""
    f = mkfolder(client, "Standards")
    fill(client, f["id"], "Autumn Leaves")

    rv = client.post(f"/api/folders/{f['id']}/assign", json={"start": start})

    assert rv.status_code == 400, f"{start!r} was accepted"
    assert db.all_slots() == []


def test_a_cleared_start_field_means_wherever_there_is_room(client):
    """`?start=` is what a cleared first-slot field renders."""
    f = mkfolder(client, "Standards")
    fill(client, f["id"], "Autumn Leaves")

    body = client.get(f"/api/folders/{f['id']}/assign?start=").get_json()

    assert body["start"] == 1


def test_folder_assign_is_covered_by_the_cross_site_guard(client):
    rv = client.post("/api/folders/1/assign",
                     headers={"Sec-Fetch-Site": "cross-site"})
    assert rv.status_code == 403
