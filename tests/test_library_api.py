"""The library HTTP surface: listing, rename, delete, assign, audition.

These run against a real Service on a throwaway data tree rather than the
duck-typed FakeService in test_web_loops, because the interesting behaviour
(refusing to delete a track a slot still holds, the collector's view of what is
still referenced) lives in the service, not the route.
"""

import pytest

from ditto import config, core, db, web

H1 = "aaaaaaaaaaaaaaaaaaaa"
H2 = "bbbbbbbbbbbbbbbbbbbb"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "state.db")
    for name in ("SOURCES", "STAGED", "TRASH", "LOOPS"):
        d = tmp_path / name.lower()
        monkeypatch.setattr(config, name, d)
        d.mkdir(parents=True, exist_ok=True)
    for attr in ("conn", "path"):
        if hasattr(db._local, attr):
            delattr(db._local, attr)

    svc = core.Service()
    svc._drain(timeout=5.0)          # let the boot sweep finish
    yield web.create_app(svc)
    svc.shutdown(timeout=2.0)
    for attr in ("conn", "path"):
        if hasattr(db._local, attr):
            delattr(db._local, attr)


@pytest.fixture
def client(app):
    return app.test_client()


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


def test_audition_is_a_safe_method_and_needs_no_guard(client):
    """A GET changes nothing, and the <audio> element can't set headers."""
    seed(H1)
    rv = client.get(f"/api/library/{H1}/audio",
                    headers={"Sec-Fetch-Site": "cross-site"})
    assert rv.status_code == 200
