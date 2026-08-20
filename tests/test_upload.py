"""POST /api/upload — the multi-file drop.

The most-used endpoint on the device and the most branch-dense, and it had no
tests at all. Driven against a fake service and a real database: the route reads
db.all_slots() directly to decide what is taken, but never needs the worker, so
there is no reason to pay for a Service here.
"""

import io

import pytest

from ditto import config, db, web


@pytest.fixture
def app_service():
    return FakeService()


@pytest.fixture
def client(app_service, tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "state.db")
    for attr in ("conn", "path"):
        if hasattr(db._local, attr):
            delattr(db._local, attr)
    app = web.create_app(app_service)
    app.config.update(TESTING=True)
    yield app.test_client()
    for attr in ("conn", "path"):
        if hasattr(db._local, attr):
            delattr(db._local, attr)


class FakeService:
    """Records what the route asked for. Nothing here touches a pedal."""

    def __init__(self):
        self.uploaded = []          # (slot, display_name)
        self.loops = set()
        self.reject = set()         # names to refuse as unreadable audio

    def has_loop(self, n):
        return n in self.loops

    def upload(self, slot, tmp_path, display_name):
        tmp_path.unlink(missing_ok=True)
        if display_name in self.reject:
            raise ValueError("not a readable audio file")
        self.uploaded.append((slot, display_name))
        return {"slot": slot, "display_name": display_name}


def f(name, body=b"audio"):
    return (io.BytesIO(body), name)


def post(client, files, **form):
    data = {"file": files}
    data.update(form)
    return client.post("/api/upload", data=data,
                       content_type="multipart/form-data")


# --- request-level rejections ----------------------------------------------

def test_no_files_is_a_400(client):
    assert client.post("/api/upload", data={},
                       content_type="multipart/form-data").status_code == 400


@pytest.mark.parametrize("start", [0, -1, 100, 999])
def test_a_start_outside_the_slot_range_is_a_400(client, start):
    rv = post(client, [f("a.mp3")], start=start)
    assert rv.status_code == 400
    assert "start must be" in rv.get_json()["error"]


# --- the filename-number rule ----------------------------------------------

def test_a_leading_number_picks_its_own_slot(app_service, client):
    """The headline "07 Blue Bossa.mp3" behaviour."""
    post(client, [f("07 Blue Bossa.mp3")])
    assert app_service.uploaded == [(7, "07 Blue Bossa")]


@pytest.mark.parametrize("name,slot", [
    ("07 Blue Bossa.mp3", 7),
    ("7 Blue Bossa.mp3", 7),
    ("007 Blue Bossa.mp3", 7),      # leading zeros collapse
    ("99 Last.mp3", 99),
    ("Track 07.mp3", 7),            # a number anywhere at the front counts
])
def test_numbers_the_rule_accepts(app_service, client, name, slot):
    post(client, [f(name)])
    assert app_service.uploaded[0][0] == slot


@pytest.mark.parametrize("name", [
    "100 Too High.mp3",             # past the last slot
    "0 Zero.mp3",                   # slots are 1-based
    "Blue Bossa.mp3",               # no number at all
])
def test_names_that_do_not_self_assign_fall_to_the_lowest_free_slot(
        app_service, client, name):
    post(client, [f(name)])
    assert app_service.uploaded[0][0] == 1


def test_a_taken_number_falls_back_rather_than_overwriting(app_service, client):
    db.library_add("a" * 20, "Sitting there", 10.0)
    db.put_slot(7, "a" * 20, state="synced")

    post(client, [f("07 Blue Bossa.mp3")])

    assert app_service.uploaded == [(1, "07 Blue Bossa")], \
        "a numbered file overwrote an occupied slot"


# --- explicit start --------------------------------------------------------

def test_start_fills_consecutive_slots(app_service, client):
    post(client, [f("a.mp3"), f("b.mp3"), f("c.mp3")], start=10)
    assert [s for s, _ in app_service.uploaded] == [10, 11, 12]


def test_start_beats_the_filename_number(app_service, client):
    post(client, [f("07 Blue Bossa.mp3")], start=30)
    assert app_service.uploaded == [(30, "07 Blue Bossa")]


def test_files_past_the_last_slot_are_reported_not_relocated(app_service,
                                                             client):
    rv = post(client, [f("a.mp3"), f("b.mp3")], start=99)
    body = rv.get_json()
    assert [s for s, _ in app_service.uploaded] == [99]
    assert len(body["errors"]) == 1
    assert "no room past slot" in body["errors"][0]["error"]


# --- a rejected file must not consume a slot -------------------------------

def test_a_numbered_non_audio_file_does_not_reserve_its_slot(app_service,
                                                             client):
    """"07 notes.txt" must not block slot 7 for the rest of the batch."""
    post(client, [f("07 notes.txt"), f("07 Blue Bossa.mp3")])

    assert app_service.uploaded == [(7, "07 Blue Bossa")]


def test_a_non_audio_file_is_reported_per_file(app_service, client):
    rv = post(client, [f("notes.txt"), f("a.mp3")])
    body = rv.get_json()
    assert rv.status_code == 201
    assert len(body["added"]) == 1
    assert body["errors"][0]["error"] == "not an audio file"


def test_an_unreadable_audio_file_frees_nothing_it_did_not_take(app_service,
                                                                client):
    app_service.reject.add("broken")
    rv = post(client, [f("broken.mp3"), f("good.mp3")])
    body = rv.get_json()
    assert [n for _, n in app_service.uploaded] == ["good"]
    assert len(body["errors"]) == 1


# --- what counts as taken --------------------------------------------------

def test_occupied_slots_are_skipped(app_service, client):
    db.library_add("a" * 20, "One", 10.0)
    db.put_slot(1, "a" * 20, state="synced")
    db.library_add("b" * 20, "Two", 10.0)
    db.put_slot(2, "b" * 20, state="synced")

    post(client, [f("x.mp3")])

    assert app_service.uploaded[0][0] == 3


def test_a_recorded_loop_reserves_its_slot_when_mounted(app_service, client,
                                                        monkeypatch):
    """We don't put a backing track under someone's performance by accident."""
    monkeypatch.setattr(web.pedal, "mounted", lambda: True)
    app_service.loops.add(1)

    post(client, [f("x.mp3")])

    assert app_service.uploaded[0][0] == 2


def test_loops_are_ignored_when_no_pedal_is_mounted(app_service, client,
                                                    monkeypatch):
    monkeypatch.setattr(web.pedal, "mounted", lambda: False)
    app_service.loops.add(1)

    post(client, [f("x.mp3")])

    assert app_service.uploaded[0][0] == 1, \
        "a stale loop cache reserved a slot with no pedal attached"


def test_a_full_pedal_reports_no_free_slots(app_service, client):
    for n in range(1, config.SLOTS + 1):
        h = f"{n:020d}"
        db.library_add(h, f"T{n}", 1.0)
        db.put_slot(n, h, state="synced")

    rv = post(client, [f("x.mp3")])

    assert app_service.uploaded == []
    assert rv.get_json()["errors"][0]["error"] == "no free slots"


# --- guards ----------------------------------------------------------------

def test_upload_is_covered_by_the_cross_site_guard(client):
    rv = client.post("/api/upload", data={"file": [f("a.mp3")]},
                     content_type="multipart/form-data",
                     headers={"Sec-Fetch-Site": "cross-site"})
    assert rv.status_code == 403
