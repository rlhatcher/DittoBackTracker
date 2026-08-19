"""SQLite state.

Three tables. `library` is the durable one: one row per uploaded track, and the
only thing that keeps its audio alive in sources/. `slots` and `trash` are
*assignments* — which library track is in which of the pedal's 99 slots, and
which slot a cleared track came from. Both reference a library row by hash and
neither carries a copy of its name or duration, so a rename has one home.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from typing import Dict, List, Optional

from . import config

log = logging.getLogger(__name__)

_local = threading.local()
# Serializes connection setup across threads. `PRAGMA journal_mode=WAL` takes an
# exclusive lock and, unlike ordinary statements, does not wait on busy_timeout
# — so several threads opening a not-yet-WAL database at the same instant will
# have all but one fail outright with "database is locked". That is exactly the
# startup shape: the worker, the monitor and the update check all connect at
# once, and on a device's very first boot the file is still in rollback-journal
# mode. Held only while opening, which happens once per thread.
_open_lock = threading.Lock()

SCHEMA_VERSION = 2

# Statements rather than one script: the initialiser runs inside BEGIN
# IMMEDIATE, and executescript() commits any open transaction before it runs,
# which would drop the very lock that serializes concurrent first-connects.
_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS library (
        source_hash TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        duration    REAL NOT NULL DEFAULT 0,
        added       REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS slots (
        slot        INTEGER PRIMARY KEY,
        source_hash TEXT NOT NULL,
        state       TEXT NOT NULL,
        synced_hash TEXT,
        error       TEXT,
        updated     REAL NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS trash (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        slot        INTEGER NOT NULL,
        source_hash TEXT NOT NULL,
        deleted     REAL NOT NULL
    )""",
]

# v1 (no library table; slots and trash carried display_name and duration) -> v2.
#
# No PRAGMA foreign_keys or legacy_alter_table handling is needed: this database
# declares no foreign key, view or trigger, so ALTER TABLE ... RENAME has nothing
# to rewrite and the drop-and-rename can't cascade. Deliberately no REFERENCES
# clause on slots.source_hash either — sqlite3 leaves foreign keys off, so it
# would buy nothing enforced while turning every future rebuild into a twelve-step
# procedure.
_V1_TO_V2 = [
    """CREATE TABLE IF NOT EXISTS library (
        source_hash TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        duration    REAL NOT NULL DEFAULT 0,
        added       REAL NOT NULL
    )""",
    # Slots win a name conflict: a track sitting in a slot shows the name the
    # user last saw. Two slots on one source resolve to the lower slot, because
    # ON CONFLICT DO NOTHING honours the SELECT's order. DO NOTHING rather than
    # INSERT OR IGNORE so it swallows only the primary-key collision and not a
    # real constraint failure.
    """INSERT INTO library (source_hash, name, duration, added)
       SELECT source_hash, display_name, duration, updated FROM slots
       ORDER BY slot
       ON CONFLICT(source_hash) DO NOTHING""",
    """INSERT INTO library (source_hash, name, duration, added)
       SELECT source_hash, display_name, duration, deleted FROM trash
       ORDER BY deleted DESC
       ON CONFLICT(source_hash) DO NOTHING""",
    """CREATE TABLE slots_v2 (
        slot        INTEGER PRIMARY KEY,
        source_hash TEXT NOT NULL,
        state       TEXT NOT NULL,
        synced_hash TEXT,
        error       TEXT,
        updated     REAL NOT NULL
    )""",
    """INSERT INTO slots_v2 (slot, source_hash, state, synced_hash, error, updated)
       SELECT slot, source_hash, state, synced_hash, error, updated FROM slots""",
    "DROP TABLE slots",
    "ALTER TABLE slots_v2 RENAME TO slots",
    """CREATE TABLE trash_v2 (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        slot        INTEGER NOT NULL,
        source_hash TEXT NOT NULL,
        deleted     REAL NOT NULL
    )""",
    # Ids carried across explicitly, not renumbered: an Undo button already
    # sitting in a browser names a specific trash id, and recycling ids would
    # re-point it at somebody else's row.
    """INSERT INTO trash_v2 (id, slot, source_hash, deleted)
       SELECT id, slot, source_hash, deleted FROM trash""",
    "DROP TABLE trash",
    "ALTER TABLE trash_v2 RENAME TO trash",
]


def _needs_v1_migration(c: sqlite3.Connection) -> bool:
    """A v1 file: has the old tables, has never been stamped with a version."""
    if c.execute("PRAGMA user_version").fetchone()[0] >= 2:
        return False
    return c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='slots'"
    ).fetchone() is not None


def _backup_v1(c: sqlite3.Connection) -> None:
    """Snapshot the database beside itself before the one destructive step.

    The recovery path if an over-the-air rollback ever strands pre-library code
    on a post-library file: `mv state.db.v1 state.db`. VACUUM INTO gives a
    consistent copy of a WAL database and cannot run inside a transaction, so
    this happens before the lock is taken — which means two threads can race
    here. The loser gets "output file already exists", which is the outcome we
    wanted anyway.
    """
    dest = str(config.DB_PATH) + ".v1"
    if os.path.exists(dest):
        return                      # a previous run (or the racing thread) made it
    try:
        c.execute("VACUUM INTO ?", (dest,))
    except sqlite3.Error as e:
        # Losing the race to another thread is the expected case and benign.
        # A full disk or an unwritable directory is not: the migration still
        # goes ahead, and the rollback path documented against it — restoring
        # state.db.v1 over state.db — will not be there. Say so, or the absence
        # is only discovered when it is needed.
        if not os.path.exists(dest):
            log.warning("could not write the pre-migration backup %s: %s. The "
                        "v1 -> v2 migration will still run, but restoring the "
                        "old schema will not be possible.", dest, e)


def _init(c: sqlite3.Connection) -> None:
    """Bring the file to SCHEMA_VERSION.

    Runs on every new connection, so the already-current case costs one PRAGMA
    read and takes no write lock.

    Version 0 is ambiguous — SQLite defaults user_version to 0 and v1 never set
    it — so a brand-new file is told apart from a v1 file by whether `slots`
    exists, not by the number. A fresh file takes the CREATE path and is stamped
    straight to SCHEMA_VERSION rather than migrating tables that already have
    the target shape.

    The worker, the monitor and any waitress request thread can first-connect in
    the same instant. BEGIN IMMEDIATE serializes them; the loser blocks on the
    write lock, then re-reads user_version inside its own transaction and finds
    the work already done.
    """
    v = c.execute("PRAGMA user_version").fetchone()[0]
    if v == SCHEMA_VERSION:
        return
    if v > SCHEMA_VERSION:
        # Almost certainly a rollback onto a newer database. Refuse loudly
        # rather than read rows whose columns this build doesn't know about.
        raise RuntimeError(
            f"{config.DB_PATH} is schema v{v}; this build understands "
            f"v{SCHEMA_VERSION}. Deploy the newer code or restore a backup.")

    if _needs_v1_migration(c):
        _backup_v1(c)

    c.execute("BEGIN IMMEDIATE")
    try:
        v = c.execute("PRAGMA user_version").fetchone()[0]   # re-read under lock
        if v >= SCHEMA_VERSION:
            c.commit()
            return
        if _needs_v1_migration(c):
            for stmt in _V1_TO_V2:
                c.execute(stmt)
        else:
            for stmt in _SCHEMA:
                c.execute(stmt)
        # An int constant, never user input — PRAGMA takes no placeholder.
        c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        c.commit()
    except Exception:
        c.rollback()
        raise


def conn() -> sqlite3.Connection:
    """One connection per thread. SQLite objects aren't shareable.

    Cached against the database path, not just the thread: a test that repoints
    config.DB_PATH would otherwise be stuck with whichever database this thread
    happened to open first.
    """
    key = str(config.DB_PATH)
    c = getattr(_local, "conn", None)
    if c is not None and getattr(_local, "path", None) == key:
        return c
    config.DATA.mkdir(parents=True, exist_ok=True)
    with _open_lock:
        c = sqlite3.connect(key, timeout=10)
        try:
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=FULL")
            _init(c)
        except Exception:
            c.close()           # don't leak the handle on a failed setup
            raise
    _local.conn, _local.path = c, key    # cache only once it has succeeded
    return c


def _row_to_dict(r: sqlite3.Row) -> Dict:
    d = dict(r)
    # `synced` is derived, never trusted from a stored flag.
    if d.get("state") == "staged" and d.get("synced_hash") == d.get("source_hash"):
        d["state"] = "synced"
    elif d.get("state") == "synced" and d.get("synced_hash") != d.get("source_hash"):
        d["state"] = "staged"
    return d


# A slot's name and duration live in library. LEFT, never inner: a slot whose
# library row somehow went missing must still appear in the grid — as a visible
# problem the user can clear — rather than silently vanishing.
_SLOT_SELECT = """
    SELECT s.*, COALESCE(l.name, '(missing)') AS display_name,
           COALESCE(l.duration, 0) AS duration
      FROM slots s LEFT JOIN library l ON l.source_hash = s.source_hash
"""


def all_slots() -> List[Dict]:
    return [_row_to_dict(r) for r in
            conn().execute(_SLOT_SELECT + " ORDER BY s.slot")]


def get_slot(slot: int) -> Optional[Dict]:
    r = conn().execute(_SLOT_SELECT + " WHERE s.slot=?", (slot,)).fetchone()
    return _row_to_dict(r) if r else None


def slots_for_hash(source_hash: str) -> List[int]:
    """Which slots hold this track. Drives the library's in-use badges, and the
    refusal to delete a track the pedal is still using."""
    return [r["slot"] for r in conn().execute(
        "SELECT slot FROM slots WHERE source_hash=? ORDER BY slot",
        (source_hash,))]


def put_slot(slot: int, source_hash: str, state: str = "converting") -> None:
    conn().execute(
        """INSERT INTO slots (slot, source_hash, state, synced_hash, error,
                              updated)
           VALUES (?,?,?,NULL,NULL,?)
           ON CONFLICT(slot) DO UPDATE SET
               source_hash=excluded.source_hash,
               state=excluded.state,
               synced_hash=NULL,
               error=NULL,
               updated=excluded.updated""",
        (slot, source_hash, state, time.time()),
    )
    conn().commit()


def set_state(slot: int, state: str, error: Optional[str] = None) -> None:
    conn().execute(
        "UPDATE slots SET state=?, error=?, updated=? WHERE slot=?",
        (state, error, time.time(), slot),
    )
    conn().commit()


def mark_synced(slot: int, source_hash: str) -> None:
    conn().execute(
        """UPDATE slots SET state='synced', synced_hash=?, error=NULL, updated=?
           WHERE slot=?""",
        (source_hash, time.time(), slot),
    )
    conn().commit()


def move_or_swap(src: int, dst: int) -> Optional[str]:
    """Move src into an empty dst, or swap two occupied slots — the choice is
    made *inside* one BEGIN IMMEDIATE transaction, after re-reading both slots.

    Deciding move-vs-swap before the transaction is racy: a concurrent upload
    could fill dst in the gap, and a plain move would then DELETE that upload.
    Holding the write lock across the read and the writes closes that window.

    Returns "move", "swap", or None (src empty, or src == dst). The caller uses
    the result to queue the right pedal work — an empty dst is erased at src and
    written at dst; a swap rewrites both.
    """
    if src == dst:
        return None
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        if not get_slot(src):
            c.commit()
            return None
        now = time.time()
        if get_slot(dst):
            # Swap. -1 is a scratch key; slot is the primary key so the two
            # updates would otherwise collide.
            c.execute("UPDATE slots SET slot=-1 WHERE slot=?", (src,))
            c.execute("UPDATE slots SET slot=? WHERE slot=?", (src, dst))
            c.execute("UPDATE slots SET slot=? WHERE slot=-1", (dst,))
            c.execute("UPDATE slots SET synced_hash=NULL, state='staged', "
                      "updated=? WHERE slot IN (?,?)", (now, src, dst))
            op = "swap"
        else:
            c.execute("DELETE FROM slots WHERE slot=?", (dst,))
            c.execute("UPDATE slots SET slot=?, synced_hash=NULL, "
                      "state='staged', updated=? WHERE slot=?", (dst, now, src))
            op = "move"
        c.commit()
        return op
    except Exception:
        c.rollback()
        raise


def delete_slot(slot: int, to_trash: bool = True) -> Optional[int]:
    """Returns the new trash id, so an undo can name the exact entry.

    BEGIN IMMEDIATE keeps the read and the delete atomic, so the trash entry
    always records the row that was actually removed. The audio is untouched:
    clearing a slot only ends an assignment, and the track stays in the library.
    """
    trash_id = None
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        row = get_slot(slot)
        if row and to_trash:
            cur = c.execute(
                "INSERT INTO trash (slot, source_hash, deleted) VALUES (?,?,?)",
                (slot, row["source_hash"], time.time()),
            )
            trash_id = cur.lastrowid
        c.execute("DELETE FROM slots WHERE slot=?", (slot,))
        c.commit()
    except Exception:
        c.rollback()
        raise
    return trash_id


def trash_items() -> List[Dict]:
    """Inner join, deliberately: an entry whose track has since been deleted
    from the library can't be restored, so it isn't offered."""
    return [dict(r) for r in conn().execute(
        """SELECT t.*, l.name AS display_name, l.duration
             FROM trash t JOIN library l ON l.source_hash = t.source_hash
         ORDER BY t.deleted DESC LIMIT 50""")]


def trash_pop(trash_id: int) -> Optional[Dict]:
    # BEGIN IMMEDIATE so the SELECT and DELETE are one atomic step: two
    # concurrent undos of the same id can't both claim the row and restore
    # the track twice.
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        r = c.execute("SELECT * FROM trash WHERE id=?", (trash_id,)).fetchone()
        if not r:
            c.commit()
            return None
        c.execute("DELETE FROM trash WHERE id=?", (trash_id,))
        c.commit()
        return dict(r)
    except Exception:
        c.rollback()
        raise


def prune_trash(max_age_days: int) -> int:
    """Bound the undo log. Frees no audio — that is the library's business
    now — so this only keeps the table from growing without limit. Returns how
    many entries went."""
    cutoff = time.time() - max_age_days * 86400
    c = conn()
    with c:
        cur = c.execute("DELETE FROM trash WHERE deleted < ?", (cutoff,))
        return cur.rowcount


# ---------------------------------------------------------------- library

def library_add(source_hash: str, name: str, duration: float) -> bool:
    """Record an uploaded track. DO NOTHING on conflict, never an upsert:
    uploading the same file again must not undo a rename.

    Returns True if this call inserted the row. The caller needs that to know
    whether a later failure is its own row to retract or somebody else's to
    leave alone.
    """
    c = conn()
    cur = c.execute(
        """INSERT INTO library (source_hash, name, duration, added)
           VALUES (?,?,?,?)
           ON CONFLICT(source_hash) DO NOTHING""",
        (source_hash, name, duration, time.time()),
    )
    c.commit()
    return cur.rowcount > 0


def library_all() -> List[Dict]:
    """Newest first. Searching, sorting and filtering happen in the browser —
    a few hundred rows is a small response and no round trip."""
    return [dict(r) for r in conn().execute(
        "SELECT * FROM library ORDER BY added DESC")]


def library_get(source_hash: str) -> Optional[Dict]:
    r = conn().execute("SELECT * FROM library WHERE source_hash=?",
                       (source_hash,)).fetchone()
    return dict(r) if r else None


def library_rename(source_hash: str, name: str) -> bool:
    c = conn()
    with c:
        cur = c.execute("UPDATE library SET name=? WHERE source_hash=?",
                        (name, source_hash))
    return cur.rowcount > 0


def library_delete(source_hash: str) -> bool:
    """Forget a track entirely. Its trash entries go in the same transaction:
    they can never be restored once the track is gone, and trash_items' inner
    join would hide them while they accumulated forever."""
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        cur = c.execute("DELETE FROM library WHERE source_hash=?",
                        (source_hash,))
        c.execute("DELETE FROM trash WHERE source_hash=?", (source_hash,))
        c.commit()
        return cur.rowcount > 0
    except Exception:
        c.rollback()
        raise


def library_hashes() -> set:
    """Every hash the library knows, as one query.

    For the collector, which would otherwise ask once per file in sources/.
    """
    return {r["source_hash"] for r in
            conn().execute("SELECT source_hash FROM library")}


def hash_in_library(source_hash: str) -> bool:
    """Is this audio still wanted? The library row is the only thing that keeps
    a file in sources/ alive — a slot assignment does not, and neither does a
    trash entry."""
    return conn().execute(
        "SELECT 1 FROM library WHERE source_hash=? LIMIT 1",
        (source_hash,)).fetchone() is not None
