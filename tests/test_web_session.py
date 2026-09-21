"""The trash routes, which the page's undo control reads."""

from ditto import db


def test_the_trash_listing_answers_before_anything_is_in_it(client):
    """GET /api/trash had no test either, and the page reads it on every undo.
    An empty device answers with a list, not null and not an error."""
    r = client.get("/api/trash")
    assert r.status_code == 200
    assert r.get_json() == []


def test_a_cleared_slot_turns_up_in_the_trash(client, service, monkeypatch):
    """What the undo control reads. The row has to carry the slot it came from
    and an id to restore by, or the page has nothing to offer."""
    monkeypatch.setattr(service, "source_for", lambda h: None)
    db.library_add("c" * 20, "Blue Bossa", 90.0)
    db.put_slot(1, "c" * 20, state="synced")

    assert client.delete("/api/slots/1").status_code == 200
    rows = client.get("/api/trash").get_json()
    assert len(rows) == 1
    assert rows[0]["slot"] == 1
    assert rows[0]["source_hash"] == "c" * 20
    assert "id" in rows[0]
