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

SCHEMA_VERSION = 3

# Statements rather than one script: the initialiser runs inside BEGIN
# IMMEDIATE, and executescript() commits any open transaction before it runs,
# which would drop the very lock that serializes concurrent first-connects.
_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS library (
        source_hash TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        duration    REAL NOT NULL DEFAULT 0,
        added       REAL NOT NULL,
        folder_id   INTEGER,
        position    INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS folders (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        name      TEXT NOT NULL,
        parent_id INTEGER,
        position  INTEGER NOT NULL DEFAULT 0,
        created   REAL NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS folders_parent ON folders(parent_id)",
    "CREATE INDEX IF NOT EXISTS library_folder ON library(folder_id)",
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


# v2 -> v3: the library gains folders.
#
# Three DDL statements and two indexes. No INSERT, no SELECT, no table rebuild —
# which is the property that makes this one safe on a device whose users cannot
# restore a backup. There is no ordering in which a power cut halfway through
# loses a row, because no row is ever read or written.
#
# It needs no backfill either. Existing tracks want folder_id NULL, which is what
# ADD COLUMN gives them, and position 0, which is the declared default. Ordering
# inside a folder is (position, added, source_hash), so rows that all sit at 0
# tie-break on `added` and keep exactly the order they have today.
#
# No "Unfiled" folder is invented for them. The footer would then report a folder
# the user never made, and the screen after an update should look like the screen
# before it.
#
# AUTOINCREMENT on folders.id for the same reason trash carries its ids across
# rather than renumbering: a browser tab left open for an hour holds folder ids
# in its DOM, and if deleting folder 7 let the next create reuse the id, that
# tab's rename would land on somebody else's folder.
#
# No REFERENCES clause, matching the rest of this file. sqlite3 leaves foreign
# keys off, so it would buy nothing enforced while turning every future rebuild
# into a twelve-step procedure. Readers tolerate a missing parent instead.
_V2_TO_V3 = [
    """CREATE TABLE IF NOT EXISTS folders (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        name      TEXT NOT NULL,
        parent_id INTEGER,
        position  INTEGER NOT NULL DEFAULT 0,
        created   REAL NOT NULL
    )""",
    "ALTER TABLE library ADD COLUMN folder_id INTEGER",
    # Legal despite NOT NULL because the default is a constant.
    "ALTER TABLE library ADD COLUMN position INTEGER NOT NULL DEFAULT 0",
    "CREATE INDEX IF NOT EXISTS folders_parent ON folders(parent_id)",
    "CREATE INDEX IF NOT EXISTS library_folder ON library(folder_id)",
]


def _needs_v1_migration(c: sqlite3.Connection) -> bool:
    """A v1 file: has the old tables, has never been stamped with a version."""
    if c.execute("PRAGMA user_version").fetchone()[0] >= 2:
        return False
    return c.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='slots'"
    ).fetchone() is not None


def _backup(c: sqlite3.Connection, tag: str) -> None:
    """Snapshot the database beside itself before migrating it.

    Named for the version on disk, not the one being migrated to, so the file
    says what it contains: a device that has been off for two releases migrates
    1 -> 2 -> 3 in one go and writes `state.db.v1`.

    The recovery path if an over-the-air rollback ever strands old code on a new
    file: `mv state.db.v1 state.db`. That matters more from v3 on than it did
    before, because _init refuses a file newer than the build outright, so a
    rolled-back v2 binary will not read a v3 file even though it could.

    VACUUM INTO gives a consistent copy of a WAL database and cannot run inside a
    transaction, so this happens before the lock is taken — which means two
    threads can race here. The loser gets "output file already exists", which is
    the outcome we wanted anyway.
    """
    dest = f"{config.DB_PATH}.{tag}"
    if os.path.exists(dest):
        return                      # a previous run (or the racing thread) made it
    try:
        c.execute("VACUUM INTO ?", (dest,))
    except sqlite3.Error as e:
        # Losing the race to another thread is the expected case and benign.
        # A full disk or an unwritable directory is not: the migration still
        # goes ahead, and the rollback path documented against it — restoring
        # the backup over state.db — will not be there. Say so, or the absence
        # is only discovered when it is needed.
        if not os.path.exists(dest):
            log.warning("could not write the pre-migration backup %s: %s. The "
                        "migration will still run, but restoring the old schema "
                        "will not be possible.", dest, e)


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
        _backup(c, "v1")
    elif v > 0:
        _backup(c, f"v{v}")

    c.execute("BEGIN IMMEDIATE")
    try:
        v = c.execute("PRAGMA user_version").fetchone()[0]   # re-read under lock
        if v >= SCHEMA_VERSION:
            c.commit()
            return
        # A chain, not a branch: a device that missed a release migrates through
        # every step in one transaction, so it is never left stamped at an
        # intermediate version that no build in the field writes.
        if _needs_v1_migration(c):
            for stmt in _V1_TO_V2:
                c.execute(stmt)
            v = 2
        elif v == 0:
            # A brand-new file. Created at the target shape, so it never runs a
            # migration step. Not `else`: a v2 file must fall through to the
            # step below, and _SCHEMA's CREATE TABLE IF NOT EXISTS would leave
            # its `library` without the new columns while looking like it had
            # worked.
            for stmt in _SCHEMA:
                c.execute(stmt)
            v = SCHEMA_VERSION
        if v < 3:
            for stmt in _V2_TO_V3:
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


# ---------------------------------------------------------------- folders

# Every walk of the tree is bounded. A LIMIT inside a recursive CTE stops SQLite
# adding rows once it is reached, so a file that somehow already contains a cycle
# returns a wrong answer instead of never returning at all. Nothing here can
# create one — folder_move refuses it — but a walk that hangs takes the worker
# thread with it, and this costs nothing.
_WALK_LIMIT = 500

# Depth-first pre-order over a subtree, as one sort key per folder.
#
# A folder's key is its parent's key with its own (position, id) appended, so a
# parent's key is a strict prefix of every descendant's. Shorter strings sort
# first, which puts a folder's own tracks ahead of all its subfolders' without
# any extra machinery, and siblings fall into position order because the segment
# is zero-padded to a fixed width.
_SUBTREE = f"""
WITH RECURSIVE tree(id, ord) AS (
    SELECT id, printf('%08d/%08d', position, id) FROM folders WHERE id = ?
  UNION ALL
    SELECT f.id, tree.ord || '/' || printf('%08d/%08d', f.position, f.id)
      FROM folders f JOIN tree ON f.parent_id = tree.id
  LIMIT {_WALK_LIMIT}
)
"""


def _depth(c: sqlite3.Connection, folder_id: Optional[int]) -> int:
    """How far below the top level a folder sits. A top-level folder is 0."""
    if folder_id is None:
        return -1                   # so a child of "nothing" comes out at 0
    row = c.execute(f"""
        WITH RECURSIVE up(id, parent_id, lvl) AS (
            SELECT id, parent_id, 0 FROM folders WHERE id = ?
          UNION ALL
            SELECT f.id, f.parent_id, up.lvl + 1
              FROM folders f JOIN up ON f.id = up.parent_id
          LIMIT {_WALK_LIMIT}
        )
        SELECT max(lvl) FROM up""", (folder_id,)).fetchone()
    return row[0] if row and row[0] is not None else -1


def folders_all() -> List[Dict]:
    """Every folder, flat. The client builds the tree and folds the counts.

    Same reasoning as library_all: a few hundred rows is a small response, and
    the browser already holds every track with its duration, so summing a subtree
    there costs one pass and no round trip. Aggregating here would also be a
    second source of truth for "16 tracks, 7 folders" that can disagree with the
    rows actually on screen, since the client filters and sorts locally.
    """
    return [dict(r) for r in conn().execute(
        "SELECT * FROM folders ORDER BY parent_id IS NOT NULL, parent_id, position, id")]


def folder_get(folder_id: int) -> Optional[Dict]:
    r = conn().execute("SELECT * FROM folders WHERE id=?", (folder_id,)).fetchone()
    return dict(r) if r else None


def folder_add(name: str, parent_id: Optional[int] = None) -> Optional[Dict]:
    """Create a folder at the end of its parent. None if the parent is unknown
    or the tree is already as deep as it may go."""
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        if parent_id is not None and not c.execute(
                "SELECT 1 FROM folders WHERE id=?", (parent_id,)).fetchone():
            c.rollback()
            return None
        if _depth(c, parent_id) + 1 >= config.MAX_FOLDER_DEPTH:
            c.rollback()
            return None
        pos = c.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM folders WHERE parent_id IS ?",
            (parent_id,)).fetchone()[0]
        cur = c.execute(
            "INSERT INTO folders (name, parent_id, position, created) VALUES (?,?,?,?)",
            (name, parent_id, pos, time.time()))
        c.commit()
        return folder_get(cur.lastrowid)
    except Exception:
        c.rollback()
        raise


def folder_rename(folder_id: int, name: str) -> bool:
    c = conn()
    with c:
        cur = c.execute("UPDATE folders SET name=? WHERE id=?", (name, folder_id))
    return cur.rowcount > 0


def folder_move(folder_id: int, parent_id: Optional[int]) -> str:
    """Reparent a folder. Returns "ok", "unknown", "cycle" or "too deep".

    The cycle check runs inside the same transaction as the UPDATE, never before
    it. Between a check and a write, another thread's reparent can make the
    answer stale, and the pair of moves that results leaves a subtree pointing
    into itself and unreachable from the top level for good.
    """
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        if not c.execute("SELECT 1 FROM folders WHERE id=?", (folder_id,)).fetchone():
            c.rollback()
            return "unknown"
        if parent_id is not None and not c.execute(
                "SELECT 1 FROM folders WHERE id=?", (parent_id,)).fetchone():
            c.rollback()
            return "unknown"
        # One walk answers both questions a reparent has: is the destination
        # inside the subtree being moved, and how tall is that subtree.
        height, contains = c.execute(f"""
            WITH RECURSIVE sub(id, lvl) AS (
                SELECT ?, 0
              UNION ALL
                SELECT f.id, sub.lvl + 1
                  FROM folders f JOIN sub ON f.parent_id = sub.id
              LIMIT {_WALK_LIMIT}
            )
            SELECT max(lvl), max(id = ?) FROM sub""",
            (folder_id, parent_id if parent_id is not None else -1)).fetchone()
        if contains:
            c.rollback()
            return "cycle"
        if _depth(c, parent_id) + 1 + (height or 0) >= config.MAX_FOLDER_DEPTH:
            c.rollback()
            return "too deep"
        pos = c.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM folders WHERE parent_id IS ?",
            (parent_id,)).fetchone()[0]
        c.execute("UPDATE folders SET parent_id=?, position=? WHERE id=?",
                  (parent_id, pos, folder_id))
        c.commit()
        return "ok"
    except Exception:
        c.rollback()
        raise


def folder_delete(folder_id: int, force: bool = False) -> Optional[Dict]:
    """Remove a folder. **Never removes a track.**

    A library row is the only thing keeping its audio alive in sources/, so a
    folder delete that took its contents with it would destroy files. A folder is
    a grouping, not a container, and this issues no DELETE against library under
    any flag. The test that pins it runs the collector afterwards.

    None if there is no such folder. Otherwise a dict saying what it found and
    whether it acted: without `force` a non-empty folder is reported and left
    alone; with it, children and tracks are promoted to the folder's own parent
    and appended there.
    """
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        row = c.execute("SELECT parent_id FROM folders WHERE id=?",
                        (folder_id,)).fetchone()
        if row is None:
            c.rollback()
            return None
        parent = row["parent_id"]
        kids = [r["id"] for r in c.execute(
            "SELECT id FROM folders WHERE parent_id=? ORDER BY position, id",
            (folder_id,))]
        tracks = c.execute("SELECT count(*) FROM library WHERE folder_id=?",
                           (folder_id,)).fetchone()[0]
        if (kids or tracks) and not force:
            c.rollback()
            return {"deleted": False, "folders": kids, "tracks": tracks}
        pos = c.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM folders WHERE parent_id IS ?",
            (parent,)).fetchone()[0]
        for i, kid in enumerate(kids):
            c.execute("UPDATE folders SET parent_id=?, position=? WHERE id=?",
                      (parent, pos + i, kid))
        tpos = c.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM library WHERE folder_id IS ?",
            (parent,)).fetchone()[0]
        c.execute("""UPDATE library SET folder_id=?,
                        position = ? + (SELECT count(*) FROM library l2
                                         WHERE l2.folder_id = library.folder_id
                                           AND (l2.added, l2.source_hash)
                                             < (library.added, library.source_hash))
                      WHERE folder_id=?""", (parent, tpos, folder_id))
        c.execute("DELETE FROM folders WHERE id=?", (folder_id,))
        c.commit()
        return {"deleted": True, "folders": kids, "tracks": tracks, "to": parent}
    except Exception:
        c.rollback()
        raise


def folder_tracks(folder_id: int) -> List[Dict]:
    """Every track in a folder and its descendants, in tree order."""
    return [dict(r) for r in conn().execute(
        _SUBTREE + """
        SELECT l.*, tree.ord FROM tree JOIN library l ON l.folder_id = tree.id
         ORDER BY tree.ord, l.position, l.added, l.source_hash""",
        (folder_id,))]


def folder_subtree_ids(folder_id: int) -> List[int]:
    """A folder and every folder under it, in tree order."""
    return [r["id"] for r in conn().execute(
        _SUBTREE + "SELECT id FROM tree ORDER BY ord", (folder_id,))]


def library_set_folder(source_hash: str, folder_id: Optional[int]) -> bool:
    """File a track, at the end of its new folder.

    The end, not wherever `added` puts it: filing an old track into a new set
    list should append it, and ordering by upload time would drop it into the
    middle.
    """
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        if folder_id is not None and not c.execute(
                "SELECT 1 FROM folders WHERE id=?", (folder_id,)).fetchone():
            c.rollback()
            return False
        pos = c.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM library WHERE folder_id IS ?",
            (folder_id,)).fetchone()[0]
        cur = c.execute("UPDATE library SET folder_id=?, position=? WHERE source_hash=?",
                        (folder_id, pos, source_hash))
        c.commit()
        return cur.rowcount > 0
    except Exception:
        c.rollback()
        raise


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


# The LEFT JOIN is what makes a missing folder harmless. This file declares no
# foreign keys, so nothing stops library.folder_id outliving the folder it names;
# taking the id from the join rather than the column reports NULL when the folder
# has gone, and the track shows up at the top level instead of in a folder the
# tree cannot draw. Same idiom as _SLOT_SELECT, where a slot whose library row
# vanished still appears.
_LIBRARY_SELECT = """
    SELECT l.source_hash, l.name, l.duration, l.added, l.position,
           f.id AS folder_id
      FROM library l LEFT JOIN folders f ON f.id = l.folder_id
"""


def library_all() -> List[Dict]:
    """Newest first. Searching, sorting and filtering happen in the browser —
    a few hundred rows is a small response and no round trip."""
    return [dict(r) for r in conn().execute(
        _LIBRARY_SELECT + "ORDER BY l.added DESC")]


def library_get(source_hash: str) -> Optional[Dict]:
    r = conn().execute(_LIBRARY_SELECT + "WHERE l.source_hash=?",
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
