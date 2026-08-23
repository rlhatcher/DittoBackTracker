"""Schema, migration and library lifetime.

The v1 -> v2 migration is the only irreversible step in the library work, and
db.py had no test file before it, so these are deliberately thorough about the
shapes the old database can be in.
"""

import sqlite3
import threading

import pytest

from ditto import config, db


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """A database path nothing has opened yet.

    db caches one connection per (thread, path), so repointing DB_PATH is enough
    to get a genuinely new database even in a process that has already used one.
    """
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "state.db")
    yield tmp_path / "state.db"
    for attr in ("conn", "path"):
        if hasattr(db._local, attr):
            delattr(db._local, attr)


V1_SCHEMA = """
CREATE TABLE slots (
    slot         INTEGER PRIMARY KEY,
    source_hash  TEXT NOT NULL,
    display_name TEXT NOT NULL,
    duration     REAL NOT NULL DEFAULT 0,
    state        TEXT NOT NULL,
    synced_hash  TEXT,
    error        TEXT,
    updated      REAL NOT NULL
);
CREATE TABLE trash (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    slot         INTEGER NOT NULL,
    source_hash  TEXT NOT NULL,
    display_name TEXT NOT NULL,
    duration     REAL NOT NULL DEFAULT 0,
    deleted      REAL NOT NULL
);
"""


def build_v1(path, slots=(), trash=()):
    """Write a database in the pre-library shape, exactly as v1 left it —
    including never stamping user_version."""
    c = sqlite3.connect(str(path))
    c.executescript(V1_SCHEMA)
    c.executemany(
        """INSERT INTO slots (slot, source_hash, display_name, duration, state,
                              synced_hash, error, updated)
           VALUES (?,?,?,?,?,?,?,?)""", slots)
    c.executemany(
        """INSERT INTO trash (id, slot, source_hash, display_name, duration,
                              deleted) VALUES (?,?,?,?,?,?)""", trash)
    c.commit()
    assert c.execute("PRAGMA user_version").fetchone()[0] == 0
    c.close()


# --- a brand-new database ---------------------------------------------------

def test_fresh_database_is_created_and_stamped(fresh_db):
    c = db.conn()
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    names = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"library", "slots", "trash"} <= names
    assert db.all_slots() == []
    assert db.library_all() == []


def test_fresh_database_leaves_no_v1_backup(fresh_db):
    """The backup exists for the migration. A new file has nothing to back up."""
    db.conn()
    assert not (fresh_db.parent / "state.db.v1").exists()


def test_reopening_a_current_database_is_a_no_op(fresh_db):
    db.conn()
    db.library_add("aaaaaaaaaaaaaaaaaaaa", "Kept", 1.5)
    delattr(db._local, "conn")
    delattr(db._local, "path")
    assert [r["name"] for r in db.library_all()] == ["Kept"]


V2_SCHEMA = """
CREATE TABLE library (
    source_hash TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    duration    REAL NOT NULL DEFAULT 0,
    added       REAL NOT NULL
);
CREATE TABLE slots (
    slot        INTEGER PRIMARY KEY,
    source_hash TEXT NOT NULL,
    state       TEXT NOT NULL,
    synced_hash TEXT,
    error       TEXT,
    updated     REAL NOT NULL
);
CREATE TABLE trash (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slot        INTEGER NOT NULL,
    source_hash TEXT NOT NULL,
    deleted     REAL NOT NULL
);
"""


def build_v2(path, library=(), slots=()):
    """Write a database in the pre-folder shape, stamped v2 as that build left
    it. The counterpart to build_v1, for the second migration."""
    c = sqlite3.connect(str(path))
    c.executescript(V2_SCHEMA)
    c.executemany(
        "INSERT INTO library (source_hash, name, duration, added) VALUES (?,?,?,?)",
        library)
    c.executemany(
        """INSERT INTO slots (slot, source_hash, state, synced_hash, error, updated)
           VALUES (?,?,?,?,?,?)""", slots)
    c.execute("PRAGMA user_version = 2")
    c.commit()
    c.close()


# --- migrating a v1 database ------------------------------------------------

def test_v1_migration_backfills_and_reshapes(fresh_db):
    build_v1(
        fresh_db,
        slots=[(3, "aaaaaaaaaaaaaaaaaaaa", "Blue Bossa", 210.0, "synced",
                "aaaaaaaaaaaaaaaaaaaa", None, 100.0)],
        trash=[(7, 12, "bbbbbbbbbbbbbbbbbbbb", "Autumn Leaves", 180.0, 90.0)],
    )

    c = db.conn()

    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    lib = {r["source_hash"]: r for r in db.library_all()}
    assert lib["aaaaaaaaaaaaaaaaaaaa"]["name"] == "Blue Bossa"
    assert lib["aaaaaaaaaaaaaaaaaaaa"]["duration"] == 210.0
    assert lib["bbbbbbbbbbbbbbbbbbbb"]["name"] == "Autumn Leaves"

    # The denormalised columns are gone from the assignment tables...
    slot_cols = {r[1] for r in c.execute("PRAGMA table_info(slots)")}
    assert "display_name" not in slot_cols and "duration" not in slot_cols
    trash_cols = {r[1] for r in c.execute("PRAGMA table_info(trash)")}
    assert "display_name" not in trash_cols and "duration" not in trash_cols

    # ...but the API still reports them, joined from library.
    row = db.get_slot(3)
    assert row["display_name"] == "Blue Bossa"
    assert row["duration"] == 210.0
    assert row["state"] == "synced"


def test_v1_migration_lets_the_slot_win_a_name_conflict(fresh_db):
    """One source in a slot and in trash under two names: the slot's is the one
    the user last saw."""
    h = "cccccccccccccccccccc"
    build_v1(
        fresh_db,
        slots=[(1, h, "Current name", 60.0, "synced", h, None, 100.0)],
        trash=[(1, 9, h, "Old name", 60.0, 200.0)],
    )

    db.conn()

    assert db.library_get(h)["name"] == "Current name"


def test_v1_migration_lets_the_lower_slot_win(fresh_db):
    """The same track in two slots resolves to the lower one, deterministically."""
    h = "dddddddddddddddddddd"
    build_v1(fresh_db, slots=[
        (9, h, "From slot nine", 60.0, "synced", h, None, 100.0),
        (2, h, "From slot two", 60.0, "synced", h, None, 100.0),
    ])

    db.conn()

    assert db.library_get(h)["name"] == "From slot two"


def test_v1_migration_preserves_trash_ids(fresh_db):
    """An Undo button already sitting in a browser names a specific trash id."""
    build_v1(
        fresh_db,
        slots=[(1, "aaaaaaaaaaaaaaaaaaaa", "A", 1.0, "synced",
                "aaaaaaaaaaaaaaaaaaaa", None, 100.0)],
        trash=[(41, 5, "bbbbbbbbbbbbbbbbbbbb", "B", 2.0, 90.0),
               (42, 6, "cccccccccccccccccccc", "C", 3.0, 95.0)],
    )

    db.conn()

    assert {r["id"] for r in db.trash_items()} == {41, 42}
    popped = db.trash_pop(42)
    assert popped["slot"] == 6


def test_v1_migration_writes_a_backup(fresh_db):
    """The recovery path if a rollback ever strands old code on a new file."""
    build_v1(fresh_db, slots=[(1, "aaaaaaaaaaaaaaaaaaaa", "A", 1.0, "synced",
                              "aaaaaaaaaaaaaaaaaaaa", None, 100.0)])

    db.conn()

    backup = fresh_db.parent / "state.db.v1"
    assert backup.exists()
    c = sqlite3.connect(str(backup))
    cols = {r[1] for r in c.execute("PRAGMA table_info(slots)")}
    assert "display_name" in cols, "the backup must be the pre-migration shape"
    c.close()


def test_v1_migration_handles_an_empty_database(fresh_db):
    build_v1(fresh_db)
    db.conn()
    assert db.all_slots() == []
    assert db.library_all() == []


def open_concurrently(n=6):
    """Have n threads first-connect at the same instant. Returns (errors, rows).

    This is the startup shape: the worker, the monitor and the update check all
    call conn() within milliseconds of each other.
    """
    errors, results = [], []
    start = threading.Barrier(n)

    def open_it():
        try:
            start.wait(timeout=5)
            results.append(len(db.library_all()))
        except Exception as e:      # noqa: BLE001 — the whole point is to see it
            errors.append(e)

    threads = [threading.Thread(target=open_it) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    return errors, results


def test_concurrent_first_connect_migrates_once(fresh_db):
    build_v1(fresh_db, slots=[(1, "aaaaaaaaaaaaaaaaaaaa", "A", 1.0, "synced",
                              "aaaaaaaaaaaaaaaaaaaa", None, 100.0)])

    errors, results = open_concurrently()

    assert errors == []
    assert results == [1] * 6


def test_concurrent_first_connect_to_a_new_database(fresh_db):
    """A device's very first boot. The file is still in rollback-journal mode,
    and `PRAGMA journal_mode=WAL` takes an exclusive lock without waiting on
    busy_timeout — so unserialized opens leave all but one thread with
    "database is locked"."""
    errors, results = open_concurrently()

    assert errors == []
    assert results == [0] * 6


def test_a_future_schema_is_refused_loudly(fresh_db):
    c = sqlite3.connect(str(fresh_db))
    c.executescript(V1_SCHEMA)
    c.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    c.commit()
    c.close()

    with pytest.raises(RuntimeError, match="schema v"):
        db.conn()


# --- lifetime: the library is what keeps audio alive ------------------------

def test_clearing_a_slot_leaves_the_track_in_the_library(fresh_db):
    h = "eeeeeeeeeeeeeeeeeeee"
    db.library_add(h, "Keeper", 120.0)
    db.put_slot(1, h, state="synced")

    db.delete_slot(1)

    assert db.get_slot(1) is None
    assert db.hash_in_library(h), "clearing a slot must not orphan the audio"


def test_pruning_trash_leaves_the_track_in_the_library(fresh_db):
    h = "ffffffffffffffffffff"
    db.library_add(h, "Keeper", 120.0)
    db.put_slot(1, h, state="synced")
    db.delete_slot(1)

    db.prune_trash(0)

    assert db.trash_items() == []
    assert db.hash_in_library(h)


def test_only_a_library_delete_releases_the_audio(fresh_db):
    h = "11111111111111111111"
    db.library_add(h, "Goodbye", 120.0)

    assert db.library_delete(h) is True
    assert not db.hash_in_library(h)


def test_library_delete_takes_the_trash_entries_with_it(fresh_db):
    """trash_items inner-joins library, so an entry left behind would be
    invisible and accumulate forever."""
    h = "22222222222222222222"
    db.library_add(h, "Goodbye", 120.0)
    db.put_slot(4, h, state="synced")
    db.delete_slot(4)

    db.library_delete(h)

    c = db.conn()
    assert c.execute("SELECT COUNT(*) FROM trash").fetchone()[0] == 0


def test_library_add_does_not_clobber_a_rename(fresh_db):
    """Re-uploading a file the user has already renamed must keep the name."""
    h = "33333333333333333333"
    db.library_add(h, "Original", 100.0)
    db.library_rename(h, "My name for it")

    db.library_add(h, "Original", 100.0)

    assert db.library_get(h)["name"] == "My name for it"


def test_slots_for_hash_reports_every_holder(fresh_db):
    h = "44444444444444444444"
    db.library_add(h, "Popular", 100.0)
    db.put_slot(9, h, state="synced")
    db.put_slot(2, h, state="synced")

    assert db.slots_for_hash(h) == [2, 9]


def test_a_slot_survives_a_missing_library_row(fresh_db):
    """LEFT join, not inner: a slot must never silently vanish from the grid."""
    h = "55555555555555555555"
    db.library_add(h, "Doomed", 100.0)
    db.put_slot(6, h, state="synced")

    db.library_delete(h)

    row = db.get_slot(6)
    assert row is not None
    assert row["display_name"] == "(missing)"


def test_trash_hides_entries_whose_track_is_gone(fresh_db):
    h = "66666666666666666666"
    db.library_add(h, "Doomed", 100.0)
    db.put_slot(6, h, state="synced")
    db.delete_slot(6)
    assert len(db.trash_items()) == 1

    db.library_delete(h)

    assert db.trash_items() == [], "an unrestorable entry must not be offered"


def test_rename_reaches_the_slot_list(fresh_db):
    h = "77777777777777777777"
    db.library_add(h, "Before", 100.0)
    db.put_slot(1, h, state="synced")

    db.library_rename(h, "After")

    assert db.get_slot(1)["display_name"] == "After"


def test_rename_of_an_unknown_track_reports_failure(fresh_db):
    db.conn()
    assert db.library_rename("88888888888888888888", "Nope") is False


# --- migrating a v2 database to v3 (folders) --------------------------------

def test_a_v2_database_gains_folders_and_keeps_every_track(fresh_db):
    """The migration is additive. Nothing about an existing track may move."""
    build_v2(fresh_db, library=[
        ("aaaaaaaaaaaaaaaaaaaa", "Blue Bossa", 311.0, 100.0),
        ("bbbbbbbbbbbbbbbbbbbb", "Autumn Leaves", 320.0, 200.0),
    ])

    rows = db.library_all()

    assert [r["name"] for r in rows] == ["Autumn Leaves", "Blue Bossa"], \
        "newest first, exactly as before"
    assert [r["duration"] for r in rows] == [320.0, 311.0]
    assert [r["added"] for r in rows] == [200.0, 100.0]
    names = {r[0] for r in db.conn().execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "folders" in names


def test_the_v2_migration_files_existing_tracks_at_the_top_level(fresh_db):
    """No invented "Unfiled" folder: the footer would report a folder the user
    never made, and the screen after an update should look like the one before."""
    build_v2(fresh_db, library=[("aaaaaaaaaaaaaaaaaaaa", "Blue Bossa", 311.0, 100.0)])

    row = db.library_get("aaaaaaaaaaaaaaaaaaaa")

    assert row["folder_id"] is None
    assert row["position"] == 0
    assert db.conn().execute("SELECT count(*) FROM folders").fetchone()[0] == 0


def test_the_v2_migration_writes_a_backup_named_for_what_it_holds(fresh_db):
    build_v2(fresh_db, library=[("aaaaaaaaaaaaaaaaaaaa", "Blue Bossa", 311.0, 100.0)])

    db.conn()

    backup = fresh_db.parent / "state.db.v2"
    assert backup.exists(), "the only way back from a v3 file"
    c = sqlite3.connect(str(backup))
    cols = {r[1] for r in c.execute("PRAGMA table_info(library)")}
    assert "folder_id" not in cols, "the backup must be the pre-migration shape"
    assert c.execute("PRAGMA user_version").fetchone()[0] == 2
    c.close()


def test_the_v2_migration_deletes_nothing(fresh_db):
    """A library row is the only thing keeping its audio alive, so a schema step
    that drops one destroys a file on the next collector pass."""
    build_v2(
        fresh_db,
        library=[("aaaaaaaaaaaaaaaaaaaa", "Held", 1.0, 100.0),
                 ("bbbbbbbbbbbbbbbbbbbb", "Loose", 2.0, 200.0)],
        slots=[(1, "aaaaaaaaaaaaaaaaaaaa", "synced", "aaaaaaaaaaaaaaaaaaaa",
                None, 100.0)])

    assert db.library_hashes() == {"aaaaaaaaaaaaaaaaaaaa", "bbbbbbbbbbbbbbbbbbbb"}
    assert [s["slot"] for s in db.all_slots()] == [1]


def test_a_v1_database_migrates_all_the_way_to_v3_in_one_open(fresh_db):
    """A device that has been off for two releases. It must never be left
    stamped at an intermediate version that no build in the field writes."""
    build_v1(fresh_db, slots=[(1, "aaaaaaaaaaaaaaaaaaaa", "A", 1.0, "synced",
                              "aaaaaaaaaaaaaaaaaaaa", None, 100.0)])

    c = db.conn()

    assert c.execute("PRAGMA user_version").fetchone()[0] == 3
    assert [r["name"] for r in db.library_all()] == ["A"]
    assert db.library_get("aaaaaaaaaaaaaaaaaaaa")["folder_id"] is None


def test_a_v1_to_v3_jump_names_its_backup_for_the_version_on_disk(fresh_db):
    """`mv state.db.v1 state.db` is the documented way back, so the file has to
    say which schema it holds, not which one it was migrating towards."""
    build_v1(fresh_db, slots=[(1, "aaaaaaaaaaaaaaaaaaaa", "A", 1.0, "synced",
                              "aaaaaaaaaaaaaaaaaaaa", None, 100.0)])

    db.conn()

    assert (fresh_db.parent / "state.db.v1").exists()
    assert not (fresh_db.parent / "state.db.v2").exists(), \
        "v2 never existed on this device"


def test_a_fresh_database_is_created_at_v3_and_never_migrates(fresh_db):
    c = db.conn()

    assert c.execute("PRAGMA user_version").fetchone()[0] == 3
    cols = {r[1] for r in c.execute("PRAGMA table_info(library)")}
    assert {"folder_id", "position"} <= cols
    assert not (fresh_db.parent / "state.db.v0").exists()
    assert not (fresh_db.parent / "state.db.v2").exists()


def test_concurrent_first_connect_to_a_v2_database_migrates_once(fresh_db):
    """The worker, the monitor and the update check all first-connect within
    milliseconds of each other on the boot after an update."""
    build_v2(fresh_db, library=[("aaaaaaaaaaaaaaaaaaaa", "A", 1.0, 100.0)])

    errors, results = open_concurrently()

    assert errors == []
    assert results == [1] * 6
    assert db.conn().execute("PRAGMA user_version").fetchone()[0] == 3


def test_reopening_a_v3_database_takes_no_write_lock(fresh_db):
    """The already-current case runs on every connection in the process, so it
    must cost one PRAGMA read and nothing else."""
    db.conn()
    for attr in ("conn", "path"):
        delattr(db._local, attr)

    blocker = sqlite3.connect(str(fresh_db))
    blocker.execute("BEGIN IMMEDIATE")      # hold the write lock
    try:
        assert db.library_all() == [], "opening must not want the write lock"
    finally:
        blocker.rollback()
        blocker.close()
