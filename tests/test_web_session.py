"""POST /api/session/end — the route that powers the device off.

end_session() itself is covered in test_busy_kind.py: the single end marker, the
refusal ordering, the admission lock. What had no test at all was the route in
front of it, which is the only way anyone actually reaches it and the only thing
standing between a LAN and the poweroff.

Safe to run because conftest._block_sudo intercepts `sudo -n /sbin/poweroff` at
import and records it instead of running it. That interception is itself pinned,
by test_hardening.test_the_suite_cannot_power_off_the_machine.
"""

import io

import conftest

from ditto import db


def test_ending_the_session_answers_ok(client):
    r = client.post("/api/session/end")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}


def test_ending_twice_still_halts_once(client, service):
    """The button is clickable until the page reloads, and a second press lands
    on a device that is already ending. It must answer rather than error, and it
    must not queue a second halt."""
    before = len(conftest.sudo_attempts)
    assert client.post("/api/session/end").status_code == 200
    assert client.post("/api/session/end").status_code == 200
    conftest.drain(service)

    poweroffs = [c for c in conftest.sudo_attempts[before:]
                 if c[-1].endswith("poweroff")]
    assert len(poweroffs) == 1, f"{len(poweroffs)} poweroffs for two presses"


def test_nothing_new_is_accepted_once_the_session_has_ended(client):
    """The point of ending is that no further work can land. A slot upload after
    it has to be refused, and with 503 rather than a generic error, because the
    device is unavailable rather than the request being wrong."""
    assert client.post("/api/session/end").status_code == 200
    r = client.post("/api/slots/1",
                    data={"file": (io.BytesIO(b"not really audio"), "x.mp3")},
                    content_type="multipart/form-data")
    assert r.status_code == 503
    assert "error" in r.get_json()


def test_ending_the_session_is_refused_cross_site(client):
    """block_cross_site is the only guard in front of the poweroff. A page on
    another origin must not be able to shut the device down, and this is the one
    route where that matters enough to name."""
    before = len(conftest.sudo_attempts)
    r = client.post("/api/session/end",
                    headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert conftest.sudo_attempts[before:] == [], "a cross-site POST reached sudo"


def test_the_poweroff_is_the_command_the_sudoers_rule_allows(client, service):
    """etc/99-ditto-poweroff grants NOPASSWD for exactly `/sbin/poweroff`. If
    the argv here ever drifts from that path, the rule stops matching and the
    device stays up with the pedal unmounted — which looks like success from the
    browser, because the response has already gone."""
    before = len(conftest.sudo_attempts)
    client.post("/api/session/end")
    conftest.drain(service)

    poweroffs = [c for c in conftest.sudo_attempts[before:]
                 if c[-1].endswith("poweroff")]
    assert poweroffs, "ending the session never reached poweroff"
    assert poweroffs[0] == ["sudo", "-n", "/sbin/poweroff"]


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
